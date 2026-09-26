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

import json
import logging
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.devin_client import DevinClient, DevinError, map_status, SessionSnapshot
from app.notifications import build_payload, destination_is_operator, fingerprint, Kind
from app.prompts import (
    branch_name,
    build_prompt,
    session_tags,
    session_title,
)
from app.slack_client import SlackTransport
from app.states import check_transition, State
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
    """The first PR owns the lifecycle; any others are recorded, not followed."""
    fields: dict[str, Any] = {}
    if not run["pr_url"]:
        fields["pr_url"] = snapshot.pull_requests[0].url
    if len(snapshot.pull_requests) > 1:
        fields["extra_pr_urls"] = json.dumps(
            [pr.url for pr in snapshot.pull_requests[1:]]
        )
    return fields


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
    ) -> None:
        self.store = store
        self.settings = settings
        self.devin = devin
        self.slack = slack
        self.owner = owner or f"{socket.gethostname()}-{id(self)}"

    # ------------------------------------------------------------------ main loop

    def run_forever(self) -> None:  # pragma: no cover - loop driver
        while True:
            worked = self.tick()
            if not worked:
                time.sleep(self.settings.poll_interval_seconds)

    def tick(self) -> bool:
        """Advance at most one run and deliver any due notifications."""
        advanced = self.advance_one()
        delivered = self.drain_outbox()
        return advanced or delivered > 0

    def advance_one(self) -> bool:
        run = self.store.claim_run(self.owner, self.settings.lease_seconds)
        if run is None:
            return False
        try:
            self._advance(run)
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

    def _record_snapshot(
        self, conn: sqlite3.Connection, run_id: str, snapshot: SessionSnapshot
    ) -> None:
        run = self.store.get_run(run_id)
        if run is None:  # pragma: no cover
            return
        task_id = int(run["task_id"])
        current = State(str(run["state"]))
        has_pr = bool(snapshot.pull_requests) or run["pr_number"] is not None
        target, reason = map_status(snapshot, has_pr)

        fields = _snapshot_fields(snapshot)

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

        if target is State.PR_OPEN and snapshot.pull_requests:
            fields.update(_pull_request_fields(run, snapshot))

        elapsed = utcnow() - datetime.fromisoformat(str(run["created_at"]))
        if (
            target not in {State.PR_OPEN, State.FAILED}
            and elapsed.total_seconds() > self.settings.run_max_seconds
        ):
            target, reason = State.EXPIRED, "expired"

        if target is not current:
            check_transition(current, target)
            fields["state"] = target.value
        if reason is not None:
            fields["failure_reason"] = reason

        self.store.update_run(conn, run_id, **fields)

        if target is not current and target in NEEDS_HUMAN_STATES:
            self._notify_state_change(
                conn, task_id=task_id, run_id=run_id, target=target, reason=reason
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

    def drain_outbox(self) -> int:
        """Send due notifications.

        A Slack failure is a notification problem and nothing else: it never
        touches the run's state, because a delivered fix that nobody was told
        about is still a delivered fix.
        """
        sent = 0
        for row in self.store.due_notifications():
            outbox_id = int(row["id"])
            destination = str(row["destination"])
            try:
                url = self.settings.webhook_url_for(destination)
            except Exception as exc:  # configuration, not transport
                self.store.mark_notification_failed(outbox_id, str(exc))
                continue

            payload: dict[str, Any] = json.loads(str(row["payload"]))
            result = self.slack.send(url, payload)
            if result.ok:
                self.store.mark_notification_sent(outbox_id, result.detail)
                sent += 1
                continue
            attempts = int(row["attempts"]) + 1
            if not result.retryable or attempts >= MAX_NOTIFICATION_ATTEMPTS:
                self.store.mark_notification_failed(outbox_id, result.detail)
                continue
            delay = result.retry_after or BASE_BACKOFF_SECONDS * (2 ** (attempts - 1))
            self.store.mark_notification_retry(outbox_id, result.detail, delay)
        return sent


def build_worker(settings: Settings, store: Store) -> Worker:
    """Assemble a worker from configuration."""
    from app.devin_client import LiveDevinClient, SimulatedDevinClient
    from app.slack_client import FakeSlackTransport, LiveSlackTransport

    devin: DevinClient = (
        LiveDevinClient(
            base_url=settings.devin_api_base,
            org_id=settings.devin_org_id,
            token=settings.devin_api_token,
        )
        if settings.devin_mode == "live"
        else SimulatedDevinClient()
    )
    slack: SlackTransport = (
        LiveSlackTransport() if settings.slack_mode == "live" else FakeSlackTransport()
    )
    return Worker(store, settings, devin, slack)


def main() -> None:  # pragma: no cover - entry point
    from app.config import load_settings

    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    store = Store(settings.database_path)
    build_worker(settings, store).run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
