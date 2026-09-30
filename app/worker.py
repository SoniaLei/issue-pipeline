#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""The worker: session creation, polling and notification delivery.

Ordering is the whole point of this module. State is written before an
external call is made, never after, so a crash between the two leaves a row
that says "a session may exist, go and check" rather than one that says
nothing and invites a second session.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.devin_client import (
    DevinClient,
    DevinError,
    map_status,
    PullRequestReview,
    SessionInsights,
    SessionSnapshot,
)
from app.notifications import (
    anchor_reaction,
    build_payload,
    destination_is_operator,
    fingerprint,
    is_follow_up,
    Kind,
)
from app.prompts import (
    branch_name,
    build_prompt,
    same_pr_url,
    session_tags,
    session_title,
)
from app.reconcile import Reconciler
from app.slack_client import SlackTransport
from app.states import check_transition, is_terminal, State
from app.store import now_iso, Store, utcnow

logger = logging.getLogger(__name__)

MAX_NOTIFICATION_ATTEMPTS = 6
BASE_BACKOFF_SECONDS = 5.0

# States where the run has stopped making progress on its own and someone has
# to look at it.
NEEDS_HUMAN_STATES = frozenset(
    {State.SESSION_BLOCKED, State.NO_OUTPUT, State.EXPIRED, State.FAILED}
)


@dataclass(frozen=True)
class BudgetVerdict:
    allowed: bool
    reason: str | None = None


def _snapshot_fields(snapshot: SessionSnapshot) -> dict[str, Any]:
    """Provider observations, kept alongside application state rather than
    collapsed into it."""
    fields: dict[str, Any] = {
        "session_id": snapshot.session_id,
        "session_url": snapshot.url,
        "session_status": snapshot.status,
        "session_status_detail": snapshot.status_detail,
        "session_polled_at": now_iso(),
    }
    if snapshot.acus_consumed is not None:
        fields["acus_consumed"] = snapshot.acus_consumed
    if snapshot.structured_output is not None:
        fields["structured_output"] = json.dumps(snapshot.structured_output)
    return fields


def _pull_request_fields(run: sqlite3.Row, snapshot: SessionSnapshot) -> dict[str, Any]:
    """The first PR owns the lifecycle; any others are recorded, not followed.

    The primary is whichever PR the run already has, not the provider's first
    entry: GitHub may have attached a different one first."""
    fields: dict[str, Any] = {}
    primary = run["pr_url"] or snapshot.pull_requests[0].url
    if not run["pr_url"]:
        fields["pr_url"] = primary
    extra: list[str] = json.loads(str(run["extra_pr_urls"] or "[]"))
    for pr in snapshot.pull_requests:
        if not same_pr_url(pr.url, primary) and not any(
            same_pr_url(pr.url, known) for known in extra
        ):
            extra.append(pr.url)
    if extra:
        fields["extra_pr_urls"] = json.dumps(extra)
    return fields


def _session_finished(insights: SessionInsights) -> bool:
    return insights.status == "exit" or insights.status_detail == "finished"


# States a pull-request webhook put the run in; session polls do not leave them.
GITHUB_OWNED: frozenset[State] = frozenset({State.PR_OPEN, State.AWAITING_REVIEW})


# Deliveries are pruned at most this often; the retention window is days.
PRUNE_INTERVAL_SECONDS = 3600
# A failed prune is retried after 1, 2 and 4 minutes, then waits for the next
# interval.
PRUNE_RETRY_BASE_SECONDS = 60
PRUNE_MAX_RETRIES = 3


class Worker:
    """One iteration of work: advance a run, then drain the outbox."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        devin: DevinClient,
        slack: SlackTransport,
        *,
        owner: str | None = None,
        reconciler: Reconciler | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.devin = devin
        self.slack = slack
        self.reconciler = reconciler
        self.owner = owner or f"{socket.gethostname()}-{id(self)}"
        self._metrics_dirty = False
        self._next_prune_at = 0.0
        self._prune_failures = 0

    # ------------------------------------------------------------------ main loop

    def run_forever(self) -> None:  # pragma: no cover - loop driver
        self.catch_up_github()
        while True:
            worked = self.tick()
            if not worked:
                time.sleep(self.settings.poll_interval_seconds)

    def tick(self) -> bool:
        """Advance at most one run, deliver due notifications, and on a tick
        with no run to advance, make one analytics read."""
        self.store.heartbeat("worker", self.owner)
        self.prune_deliveries()
        advanced = self.advance_one()
        delivered = self.drain_outbox()
        reconciled = reviewed = False
        if not advanced:
            # GitHub reconciliation re-reads one active run whose issue and PR
            # are due a look (D-041). Idle ticks only: a slow GitHub delays no
            # run, and a failing read must never stop runs from advancing.
            try:
                reconciled = self.reconcile_github()
            except Exception:
                logger.exception("GitHub reconciliation failed")
        if not advanced and not reconciled:
            # The review gate reads and requests a provider review of the
            # PR's current head. Like analytics it runs on an idle tick and a
            # failing provider must not stop runs from advancing.
            try:
                reviewed = self.refresh_review_gate()
            except Exception:
                logger.exception("review gate refresh failed")
        if (
            self.settings.analytics_enabled
            and not advanced
            and not reconciled
            and not reviewed
        ):
            # Analytics are observation, not control: they wait for an idle
            # tick so a slow provider endpoint delays no run, and a broken
            # one must never stop runs from advancing.
            try:
                self.refresh_analytics()
            except Exception:
                logger.exception("analytics refresh failed")
        return advanced or delivered > 0 or reconciled

    def reconcile_github(self) -> bool:
        """One GitHub re-read, when configured and a run is due one."""
        if self.reconciler is None:
            return False
        return self.reconciler.reconcile_one()

    def catch_up_github(self) -> int:
        """Startup pass over every active run (D-041). A GitHub outage at
        boot is logged, not fatal: the interval pass retries."""
        if self.reconciler is None:
            return 0
        try:
            looked = self.reconciler.catch_up()
        except Exception:
            logger.exception("GitHub startup reconciliation failed")
            return 0
        logger.info("startup reconciliation re-read %d active run(s)", looked)
        return looked

    def prune_deliveries(self) -> int:
        """Drop raw delivery bodies older than DELIVERY_RETENTION_DAYS (D-014),
        at most once per PRUNE_INTERVAL_SECONDS. Zero or less keeps them all."""
        now = time.monotonic()
        if self.settings.delivery_retention_days <= 0 or now < self._next_prune_at:
            return 0
        try:
            removed = self.store.prune_deliveries(self.settings.delivery_retention_days)
        except sqlite3.Error:
            self._prune_failures += 1
            if self._prune_failures > PRUNE_MAX_RETRIES:
                self._prune_failures = 0
                delay = PRUNE_INTERVAL_SECONDS
            else:
                delay = PRUNE_RETRY_BASE_SECONDS * 2 ** (self._prune_failures - 1)
            self._next_prune_at = now + delay
            logger.exception("pruning deliveries failed; next attempt in %ds", delay)
            return 0
        self._prune_failures = 0
        self._next_prune_at = now + PRUNE_INTERVAL_SECONDS
        if removed:
            logger.info("pruned %d deliveries", removed)
        return removed

    def advance_one(self) -> bool:
        run = self.store.claim_run(self.owner, self.settings.lease_seconds)
        if run is None:
            return False
        try:
            self._advance(run)
        except Exception:
            # One run's bad day must not stop every other run from moving;
            # the row stays as it was and is picked up again next tick.
            logger.exception("advancing run %s failed", run["id"])
        finally:
            self.store.release_run(str(run["id"]))
        return True

    def _advance(self, run: sqlite3.Row) -> None:
        state = State(str(run["state"]))
        if state is State.QUEUED:
            self._start(run)
        elif state in {State.STARTING, State.RUNNING, State.SESSION_BLOCKED}:
            self._poll(run)

    # -------------------------------------------------------------------- budgets

    def check_budget(self, repo: str) -> BudgetVerdict:
        """Limits are checked before creation, never after.

        A run that exceeds one stays queued rather than failing: the
        authorization it carries is still valid tomorrow, and a silently
        dropped run is indistinguishable from a bug to the maintainer who
        approved it.
        """
        if self.store.count_active_runs(repo) >= self.settings.max_concurrent_runs:
            return BudgetVerdict(False, "concurrency")
        since = utcnow() - timedelta(days=1)
        if (
            self.store.count_sessions_started_since(repo, since)
            >= self.settings.max_daily_sessions
        ):
            return BudgetVerdict(False, "daily_cap")
        return BudgetVerdict(True)

    # ------------------------------------------------------------ session startup

    def _start(self, run: sqlite3.Row) -> None:
        run_id = str(run["id"])
        task = self.store.get_task(int(run["task_id"]))
        if task is None:  # pragma: no cover - foreign key makes this impossible
            return
        repo = str(task["repo"])

        verdict = self.check_budget(repo)
        if not verdict.allowed:
            logger.info("run %s held in queue: %s", run_id, verdict.reason)
            with self.store.transaction() as conn:
                self.store.update_run(conn, run_id, failure_reason=verdict.reason)
            return

        issue = json.loads(str(run["input_snapshot"] or "{}"))
        issue_number = int(task["issue_number"])
        branch = branch_name(issue_number, run_id)
        tags = session_tags(
            run_id=run_id,
            repo=repo,
            issue_number=issue_number,
            env=str(run["env"]),
        )

        # Written before the call, so an ambiguous response has something to
        # reconcile against.
        with self.store.transaction() as conn:
            check_transition(State(str(run["state"])), State.STARTING)
            self.store.update_run(
                conn,
                run_id,
                state=State.STARTING.value,
                branch=branch,
                failure_reason=None,
            )

        prompt = build_prompt(
            repo=repo,
            issue=issue or {"number": issue_number, "title": task["issue_title"]},
            run_id=run_id,
            base_sha=str(run["base_sha"] or "") or None,
            branch=branch,
        )
        max_acu = int(run["max_acu_limit"] or self.settings.max_acu_limit)

        try:
            snapshot = self.devin.create_session(
                prompt=prompt,
                title=session_title(repo, issue_number, str(task["issue_title"])),
                repo_url=f"https://github.com/{repo}",
                tags=tags,
                max_acu_limit=max_acu,
                playbook_id=self.settings.devin_playbook_id or None,
                secret_ids=list(self.settings.devin_secret_ids),
            )
        except DevinError as exc:
            self._handle_create_failure(run_id, int(task["id"]), run_id, exc)
            return

        with self.store.transaction() as conn:
            self._record_snapshot(conn, run_id, snapshot)

    def _handle_create_failure(
        self, run_id: str, task_id: int, tag_run_id: str, exc: DevinError
    ) -> None:
        """Resolve an ambiguous create before considering a retry.

        A timeout does not mean nothing happened. Retrying blind is how one
        approval becomes two sessions and two bills, so the tag set at
        creation is used to ask the provider what actually exists.
        """
        if exc.retryable:
            try:
                found = self.devin.find_session_by_tag(f"run:{tag_run_id}")
            except DevinError:
                found = None
            if found is not None:
                with self.store.transaction() as conn:
                    self._record_snapshot(conn, run_id, found)
                return
            with self.store.transaction() as conn:
                self.store.update_run(
                    conn,
                    run_id,
                    state=State.QUEUED.value,
                    failure_reason="create_retry",
                )
            return

        with self.store.transaction() as conn:
            self.store.update_run(
                conn,
                run_id,
                state=State.FAILED.value,
                failure_reason="create_failed",
            )
            self._notify(
                conn,
                task_id=task_id,
                run_id=run_id,
                kind=Kind.NEEDS_HUMAN,
                reason="session_error",
                revision=run_id,
                detail=str(exc),
            )

    # ------------------------------------------------------------------- polling

    def _poll(self, run: sqlite3.Row) -> None:
        run_id = str(run["id"])
        session_id = run["session_id"]
        if not session_id:
            # Claimed in `starting` with no session recorded: the create never
            # completed. Back to the queue, where the budget check applies again.
            with self.store.transaction() as conn:
                self.store.update_run(conn, run_id, state=State.QUEUED.value)
            return
        try:
            snapshot = self.devin.get_session(str(session_id))
        except DevinError as exc:
            logger.warning("poll failed for %s: %s", run_id, exc)
            return
        with self.store.transaction() as conn:
            self._record_snapshot(conn, run_id, snapshot)

    def _resolve_target(
        self, run: sqlite3.Row, snapshot: SessionSnapshot, fields: dict[str, Any]
    ) -> tuple[State, str | None]:
        """What the provider's report, plus our own budgets, says the run is."""
        has_pr = bool(snapshot.pull_requests) or run["pr_number"] is not None
        target, reason = map_status(snapshot, has_pr)

        if reason == "finished_no_pr":
            fields["session_finished_at"] = run["session_finished_at"] or now_iso()
            target, reason = self._grace_verdict(run, fields["session_finished_at"])

        max_acu = run["max_acu_limit"]
        if (
            max_acu is not None
            and snapshot.acus_consumed is not None
            and float(snapshot.acus_consumed) >= float(max_acu)
            and not has_pr
        ):
            # The ceiling is provider-enforced; observing it here is only so
            # the run is reported honestly and never auto-retried, because an
            # identical retry fails identically at the same price.
            target, reason = State.FAILED, "acu_limit"

        elapsed = utcnow() - datetime.fromisoformat(str(run["created_at"]))
        if (
            target not in {State.PR_OPEN, State.FAILED}
            and elapsed.total_seconds() > self.settings.run_max_seconds
        ):
            target, reason = State.EXPIRED, "expired"
        return target, reason

    def _record_snapshot(
        self, conn: sqlite3.Connection, run_id: str, snapshot: SessionSnapshot
    ) -> None:
        run = self.store.get_run(run_id)
        if run is None:  # pragma: no cover
            return
        task_id = int(run["task_id"])
        current = State(str(run["state"]))
        fields = _snapshot_fields(snapshot)
        target, reason = self._resolve_target(run, snapshot, fields)

        if target is State.PR_OPEN and snapshot.pull_requests:
            fields.update(_pull_request_fields(run, snapshot))

        if current in GITHUB_OWNED:
            # Once a PR exists its lifecycle belongs to GitHub events. The
            # session is still observed for ACUs and structured output, but
            # whatever it reports about itself moves the run nowhere: the
            # webhook that opened the PR may land between claiming this run
            # and recording the poll.
            target, reason = current, None

        if target is not current:
            check_transition(current, target)
            fields["state"] = target.value
        if reason is not None:
            fields["failure_reason"] = reason

        self.store.update_run(conn, run_id, **fields)

        provider_before = (run["session_status"], run["session_status_detail"])
        provider_after = (snapshot.status, snapshot.status_detail)
        if run["session_id"] is None or provider_before != provider_after:
            # Provider status is recorded on change, never per poll: the
            # timeline should read as a story, not as a heartbeat log.
            self.store.record_event(
                conn,
                run_id=run_id,
                task_id=task_id,
                kind="session",
                reason="created" if run["session_id"] is None else "status",
                detail={
                    "session_id": snapshot.session_id,
                    "url": snapshot.url,
                    "status": snapshot.status,
                    "status_detail": snapshot.status_detail,
                    "acus_consumed": snapshot.acus_consumed,
                    "structured_output": snapshot.structured_output,
                },
            )

        if target is not current and target in NEEDS_HUMAN_STATES:
            self._notify_state_change(
                conn, task_id=task_id, run_id=run_id, target=target, reason=reason
            )

        # Without the primary's webhook there is no "PR opened" anchor yet; that
        # webhook sends these once it has queued one. The fingerprint makes it
        # once per extra PR either way.
        if run["pr_number"] is not None:
            for url in json.loads(fields.get("extra_pr_urls", "[]")):
                self._notify(
                    conn,
                    task_id=task_id,
                    run_id=run_id,
                    kind=Kind.NEEDS_HUMAN,
                    reason="scope",
                    revision=f"scope:{url}",
                    detail=str(url),
                )

    def _notify_state_change(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
        run_id: str,
        target: State,
        reason: str | None,
    ) -> None:
        self._notify(
            conn,
            task_id=task_id,
            run_id=run_id,
            kind=Kind.NEEDS_HUMAN,
            reason=reason,
            revision=f"{target.value}:{reason}",
        )

    def _grace_verdict(
        self, run: sqlite3.Row, finished_at: str
    ) -> tuple[State, str | None]:
        """A finished session with no PR is not immediately a dead run.

        The PR webhook and the session poll race, and calling `no_output` on
        the first observation would announce a failure that a maintainer can
        already see a pull request for.
        """
        finished = datetime.fromisoformat(finished_at)
        if finished.tzinfo is None:  # pragma: no cover - defensive
            finished = finished.replace(tzinfo=timezone.utc)
        if (
            utcnow() - finished
        ).total_seconds() < self.settings.no_output_grace_seconds:
            return State(str(run["state"])), None
        return State.NO_OUTPUT, "no_output"

    # --------------------------------------------------------------- notifications

    def _notify(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
        run_id: str | None,
        kind: Kind,
        reason: str | None,
        revision: str | None,
        detail: str | None = None,
    ) -> None:
        task = self.store.get_task(task_id)
        if task is None:  # pragma: no cover
            return
        run = self.store.get_run(run_id) if run_id else None
        destination = self.settings.destination_for(
            str(task["repo"]), operator=destination_is_operator(reason)
        )
        self.store.enqueue_notification(
            conn,
            task_id=task_id,
            run_id=run_id,
            kind=kind.value,
            reason=reason,
            fingerprint=fingerprint(
                task_id=task_id, kind=kind, destination=destination, revision=revision
            ),
            destination=destination,
            payload=build_payload(
                kind=kind, task=task, run=run, reason=reason, detail=detail
            ),
        )

    # ---------------------------------------------------------------- review gate

    def refresh_review_gate(self) -> bool:
        """Make one pr-reviews read for a run whose current head is due.

        A run is due when GitHub owns it (``pr_open``/``awaiting_review``),
        its head has no review row or one whose API status is not terminal,
        and the last read is older than the poll interval. When no review
        exists for the head one is requested, once: the head is claimed in the
        store before the request is sent, so a second worker or a restart
        does not ask again. Reads that keep failing stop after
        ``review_max_attempts``, and a permanent error (``retryable`` false,
        e.g. a missing permission) stops on the first; either leaves the head
        ``unavailable`` with the error, which the dashboard shows and which
        never counts as a pass (D-033).
        """
        if self.settings.review_gate_mode == "off":
            return False
        now = utcnow()
        for run in self.store.review_candidates(self.settings.env):
            status_at = run["review_status_at"]
            if status_at is not None:
                read = datetime.fromisoformat(str(status_at))
                if (now - read).total_seconds() < self.settings.review_poll_seconds:
                    continue
            given_up = (
                run["review_status"] == "unavailable"
                and int(run["review_attempts"] or 0)
                >= self.settings.review_max_attempts
            )
            if given_up:
                continue
            self._refresh_review(run)
            return True
        return False

    def _refresh_review(self, run: sqlite3.Row) -> None:
        run_id = str(run["id"])
        head = str(run["head_sha"])
        pr_url = str(run["pr_url"])
        current = self.store.review_for_head(run_id, head)
        previous_status = current["status"] if current is not None else None
        attempts = int(current["attempts"]) if current is not None else 0
        requested = current is not None and current["requested_at"] is not None
        fields: dict[str, Any] = {
            "status_at": now_iso(),
            "last_error": None,
            "retryable": 1,
        }
        try:
            review = self.devin.get_pr_review(pr_url, head)
            if review is None and not requested:
                review = self._request_review(run, fields)
            if review is not None:
                fields["status"] = review.status
                fields["attempts"] = 0
            elif "status" not in fields:
                # Requested earlier but not yet visible: still pending, and
                # each empty read counts against the attempt budget.
                attempts += 1
                fields["attempts"] = attempts
                if attempts >= self.settings.review_max_attempts:
                    fields["status"] = "unavailable"
                    fields["last_error"] = "requested review never appeared"
                else:
                    fields["status"] = "pending"
        except DevinError as exc:
            logger.warning("review gate read failed for %s: %s", run_id, exc)
            attempts += 1
            fields["attempts"] = attempts
            fields["last_error"] = str(exc)
            if not exc.retryable:
                fields["status"] = "unavailable"
                fields["retryable"] = 0
            elif attempts >= self.settings.review_max_attempts:
                fields["status"] = "unavailable"
            elif previous_status is not None:
                fields["status"] = previous_status
        self._record_review(run, head, previous=previous_status, **fields)

    def _request_review(
        self, run: sqlite3.Row, fields: dict[str, Any]
    ) -> PullRequestReview | None:
        """Ask Devin for a review of the run's head, once across workers.

        The head is claimed in the store first; a worker that loses the claim
        makes no request and reads again next tick. A request that fails
        gives the claim back, so the next read (GET first, which finds any
        review Devin did accept) may ask again. Returns the review for this
        head, or None when none is available yet.
        """
        run_id = str(run["id"])
        head = str(run["head_sha"])
        pr_url = str(run["pr_url"])
        requested_at = now_iso()
        claimed = self.store.claim_review_request(
            run_id=run_id,
            head_sha=head,
            env=str(run["env"]),
            pr_url=pr_url,
            requested_at=requested_at,
        )
        if not claimed:
            return None
        try:
            review = self.devin.request_pr_review(pr_url)
        except DevinError:
            self.store.release_review_request(run_id, head)
            raise
        fields["requested_at"] = requested_at
        if review.commit_sha and review.commit_sha != head:
            # Devin reviews whatever GitHub's latest commit is. If that is not
            # the head this run knows, GitHub has moved on and the synchronize
            # delivery will bring the new head, whose row this is; the head
            # asked about gets no review.
            self._record_review(
                run,
                review.commit_sha,
                previous=None,
                status=review.status,
                requested_at=requested_at,
                status_at=fields["status_at"],
            )
            fields["status"] = "skipped"
            fields["last_error"] = (
                f"Devin reviewed newer commit {review.commit_sha[:10]};"
                " waiting for GitHub to report that head"
            )
            return None
        return review

    def _record_review(
        self,
        run: sqlite3.Row,
        head: str,
        *,
        previous: str | None,
        **fields: Any,
    ) -> None:
        run_id = str(run["id"])
        status = fields.get("status")
        with self.store.transaction() as conn:
            self.store.upsert_review(
                conn,
                run_id=run_id,
                head_sha=head,
                env=str(run["env"]),
                pr_url=str(run["pr_url"]),
                **fields,
            )
            if status is not None and status != previous:
                self.store.record_event(
                    conn,
                    run_id=run_id,
                    task_id=int(run["task_id"]),
                    kind="review_gate",
                    reason=str(status),
                    detail={
                        "head_sha": head,
                        "requested": "requested_at" in fields,
                        "error": fields.get("last_error"),
                    },
                )

    # ------------------------------------------------------------------ analytics

    def refresh_analytics(self) -> bool:
        """Read Devin's account of one session, or the org counts when due.

        Everything read here is stored beside the run, never applied to it:
        the run's state comes from GitHub and the session poll, the numbers
        here say what the run cost and what the provider thinks of it. One
        read per tick keeps the worker's time in analytics bounded by a few
        short-timeout calls.
        """
        env = self.settings.env
        now = utcnow()
        cutoff = now - timedelta(seconds=self.settings.insights_refresh_seconds)
        candidates = self.store.insights_candidates(env, cutoff)
        if candidates:
            self._refresh_insights(candidates[0], now)
            # A run moving is the moment the two sides can diverge, so the
            # counts are re-read on the next idle tick as well as on the interval.
            self._metrics_dirty = True
            return True
        # Nothing to filter on until a session has said which identity it ran
        # as; unfiltered org numbers would count every human's session as
        # pipeline work.
        if self.store.service_user_ids(env) and (
            self._metrics_dirty or self._metrics_due(env, now)
        ):
            self._refresh_metrics(env, now)
            self._metrics_dirty = False
            return True
        return False

    def _refresh_insights(self, run: sqlite3.Row, now: datetime) -> None:
        run_id = str(run["id"])
        session_id = str(run["session_id"])
        fields: dict[str, Any] = {
            "session_id": session_id,
            "env": str(run["env"]),
            "run_state": run["state"],
            "session_status": run["session_status"],
            "session_status_detail": run["session_status_detail"],
            "last_error": None,
        }
        try:
            insights = self.devin.get_session_insights(session_id)
        except DevinError as exc:
            logger.warning("insights read failed for %s: %s", run_id, exc)
            fields["last_error"] = str(exc)
            with self.store.transaction() as conn:
                self.store.upsert_session_insights(conn, run_id, **fields)
            return

        fields.update(
            acus_consumed=insights.acus_consumed,
            session_size=insights.session_size,
            category=insights.category,
            subcategory=insights.subcategory,
            origin=insights.origin,
            service_user_id=insights.service_user_id,
            num_user_messages=insights.num_user_messages,
            num_devin_messages=insights.num_devin_messages,
            analysis_status=insights.analysis_status,
            analysis=(
                json.dumps(insights.analysis) if insights.analysis is not None else None
            ),
        )
        try:
            consumption = self.devin.get_session_consumption(session_id)
            fields["billed_acus"] = consumption.total_acus
            fields["consumption"] = json.dumps(list(consumption.by_date))
        except DevinError as exc:
            logger.warning("consumption read failed for %s: %s", run_id, exc)
            fields["last_error"] = str(exc)

        previous = self.store.insights_for_run(run_id)
        finished = _session_finished(insights) or is_terminal(State(str(run["state"])))
        if (
            finished
            and insights.analysis_status in {None, "not_started"}
            and (previous is None or previous["generate_requested_at"] is None)
        ):
            # Small sessions are not analysed unless asked; asking is free and
            # idempotent (the provider answers `already_exists` the second time).
            try:
                self.devin.request_session_insights(session_id)
                fields["generate_requested_at"] = now_iso()
            except DevinError as exc:
                logger.warning("insights generate failed for %s: %s", run_id, exc)
                fields["last_error"] = str(exc)

        # The settle window is a deadline, not a wait for the provider: a
        # terminal run stops being re-read once it has passed, whatever the
        # analysis status ended up as. Whatever was read last stands.
        ended = datetime.fromisoformat(str(run["updated_at"]))
        if ended.tzinfo is None:  # pragma: no cover - defensive
            ended = ended.replace(tzinfo=timezone.utc)
        fields["settled"] = int(
            is_terminal(State(str(run["state"])))
            and (now - ended).total_seconds() > self.settings.insights_settle_seconds
        )

        with self.store.transaction() as conn:
            self.store.upsert_session_insights(conn, run_id, **fields)
            newly_analysed = insights.analysis_status == "completed" and (
                previous is None or previous["analysis_status"] != "completed"
            )
            if newly_analysed:
                analysis = insights.analysis or {}
                self.store.record_event(
                    conn,
                    run_id=run_id,
                    task_id=int(run["task_id"]),
                    kind="insights",
                    reason="analysed",
                    detail={
                        "session_size": insights.session_size,
                        "category": insights.category,
                        "acus_consumed": insights.acus_consumed,
                        "billed_acus": fields.get("billed_acus"),
                        "issues": len(analysis.get("issues") or []),
                        "action_items": len(analysis.get("action_items") or []),
                    },
                )

    def _metrics_due(self, env: str, now: datetime) -> bool:
        row = self.store.provider_metrics(env)
        if row is None:
            return True
        attempted = datetime.fromisoformat(str(row["attempted_at"]))
        return (
            now - attempted
        ).total_seconds() >= self.settings.metrics_refresh_seconds

    def _refresh_metrics(self, env: str, now: datetime) -> None:
        ids = self.store.service_user_ids(env)
        earliest = self.store.earliest_run_created_at(env)
        after = datetime.fromisoformat(earliest) if earliest else now
        if after.tzinfo is None:  # pragma: no cover - defensive
            after = after.replace(tzinfo=timezone.utc)
        after -= timedelta(hours=1)
        metrics: dict[str, Any] | None = None
        error: str | None = None
        try:
            result = self.devin.get_provider_metrics(
                service_user_ids=ids, time_after=after, time_before=now
            )
            metrics = dataclasses.asdict(result)
        except DevinError as exc:
            logger.warning("provider metrics read failed: %s", exc)
            error = str(exc)
        self.store.save_provider_metrics(
            env,
            window_after=after,
            window_before=now,
            service_user_ids=ids,
            metrics=metrics,
            error=error,
        )

    # --------------------------------------------------------------- notifications

    def drain_outbox(self) -> int:
        """Send due notifications.

        A Slack failure is a notification problem and nothing else: it never
        touches the run's state, because a delivered fix that nobody was told
        about is still a delivered fix.

        A run's "PR opened" message is the anchor for what follows it: later
        messages for the run go into its thread and add a reaction to it, so
        the channel reads one line per PR. A follow-up whose anchor is still
        queued waits for it; one whose anchor never got a timestamp (webhook
        transport, failed anchor, history from before anchors) goes top-level
        rather than being lost. Slack holds no state here that GitHub does
        not: the anchor is a place to put words, never a source of truth.
        """
        sent = 0
        for row in self.store.due_notifications():
            outbox_id = int(row["id"])
            destination = str(row["destination"])
            try:
                target = self.settings.slack_target_for(destination)
            except Exception as exc:  # configuration, not transport
                self.store.mark_notification_failed(outbox_id, str(exc))
                continue

            kind = Kind(str(row["kind"]))
            run_id = str(row["run_id"]) if row["run_id"] else None
            anchor: sqlite3.Row | None = None
            if run_id and self.slack.threads and is_follow_up(kind):
                anchor = self.store.slack_anchor(run_id, destination)
                if anchor is None and self.store.slack_anchor_pending(
                    run_id, destination
                ):
                    continue
            thread_ts = str(anchor["slack_ts"]) if anchor is not None else None

            payload: dict[str, Any] = json.loads(str(row["payload"]))
            result = self.slack.send(target, payload, thread_ts=thread_ts)
            if result.ok:
                self.store.mark_notification_sent(
                    outbox_id,
                    result.detail,
                    channel=result.channel,
                    ts=result.ts,
                    thread_ts=thread_ts,
                )
                sent += 1
                if anchor is not None:
                    self._react_on_anchor(outbox_id, anchor, kind, row["reason"])
                continue
            attempts = int(row["attempts"]) + 1
            if not result.retryable or attempts >= MAX_NOTIFICATION_ATTEMPTS:
                self.store.mark_notification_failed(outbox_id, result.detail)
                continue
            delay = result.retry_after or BASE_BACKOFF_SECONDS * (2 ** (attempts - 1))
            self.store.mark_notification_retry(outbox_id, result.detail, delay)
        return sent

    def _react_on_anchor(
        self, outbox_id: int, anchor: sqlite3.Row, kind: Kind, reason: str | None
    ) -> None:
        """Best effort, once: the reply is the record, the reaction is a glance.

        A reaction that fails is recorded on the reply's row and not retried;
        retrying would re-send nothing useful and the thread already carries
        the information.
        """
        name = anchor_reaction(kind, reason)
        if name is None:
            return
        result = self.slack.react(
            str(anchor["slack_channel"]), str(anchor["slack_ts"]), name
        )
        self.store.mark_notification_reaction(
            outbox_id, name, None if result.ok else result.detail
        )


def build_worker(settings: Settings, store: Store) -> Worker:
    """Assemble a worker from configuration."""
    from app.devin_client import LiveDevinClient, SimulatedDevinClient
    from app.github_client import LiveGitHubClient
    from app.intake import Intake
    from app.slack_client import (
        BotSlackTransport,
        FakeSlackTransport,
        LiveSlackTransport,
    )

    devin: DevinClient = (
        LiveDevinClient(
            base_url=settings.devin_api_base,
            org_id=settings.devin_org_id,
            token=settings.devin_api_token,
        )
        if settings.devin_mode == "live"
        else SimulatedDevinClient()
    )
    slack: SlackTransport
    if settings.slack_mode != "live":
        slack = FakeSlackTransport(threads=settings.slack_transport == "bot")
    elif settings.slack_transport == "bot":
        slack = BotSlackTransport(settings.slack_bot_token)
    else:
        slack = LiveSlackTransport()
    reconciler: Reconciler | None = None
    if settings.github_mode == "live":
        github = LiveGitHubClient(
            base_url=settings.github_api_base, token=settings.github_token
        )
        reconciler = Reconciler(store, settings, Intake(store, settings), github)
    return Worker(store, settings, devin, slack, reconciler=reconciler)


def main() -> None:  # pragma: no cover - entry point
    from app.config import load_settings

    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    store = Store(settings.database_path)
    build_worker(settings, store).run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
