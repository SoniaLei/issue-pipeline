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
"""GitHub reconciliation: the half of the safety net webhooks cannot be (D-041).

A webhook is a hint that something happened. It can be dropped while the
service is down, retried past GitHub's window and then given up on, or simply
never sent because the tunnel URL changed. Nothing here trusts that every
event arrived. Instead, on an interval, each *active* run's issue and pull
request are re-read from the API and whatever differs from the recorded
state is applied.

Applied *through the intake*, not beside it. The reconciler synthesises the
webhook delivery GitHub would have sent — same event, same action, same
object — and hands it to ``Intake.handle`` under a deterministic delivery id.
Authorization, ordering (D-040), terminal-state protection (§11), the outbox
and the timeline therefore have one implementation, and a reconciled change
is indistinguishable from a delivered one except for the ``reconcile:`` prefix
on its delivery row.

Two rules bound what the API can make the service do:

- It never authorizes spend on its own. An approval needs the actor who
  applied the label, which the issue object does not carry; the reconciler
  reads the issue's event log for the ``labeled`` event and names *that*
  actor as the sender, so the allowlist check in the intake still decides.
  No event, no approval.
- It never moves a terminal run. Candidates are active runs only, and the
  intake's own rules refuse to reopen what GitHub has closed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.github_client import GitHubClient, GitHubError, MAX_PAGES, PAGE_SIZE
from app.intake import HUMAN_REVIEW_OUTCOMES, Intake
from app.review_gate import is_review_bot
from app.states import can_transition, is_terminal, State
from app.store import now_iso, Store, utcnow

logger = logging.getLogger(__name__)

DELIVERY_PREFIX = "reconcile"
# The live client reads at most this many issue events (oldest first).
MAX_ISSUE_EVENTS = MAX_PAGES * PAGE_SIZE

# What a fetched pull's draft flag becoming true/false would have been
# delivered as. Neither announces anything; `ready_for_review` does.
_DRAFT_ACTIONS = {True: "converted_to_draft", False: "ready_for_review"}


def _labels(issue: dict[str, Any]) -> list[str]:
    return [str(label.get("name", "")) for label in issue.get("labels") or []]


def _login(obj: Any) -> str:
    return str(((obj or {}).get("login")) or "")


def _as_open(pull: dict[str, Any]) -> dict[str, Any]:
    """The snapshot as the ``opened``/``synchronize`` webhook carried it: a
    PR is open when it opens, whatever became of it since."""
    return {**pull, "state": "open"}


def _is_successful_suite(suite: dict[str, Any]) -> bool:
    return (
        str(suite.get("status") or "") == "completed"
        and str(suite.get("conclusion") or "") == "success"
    )


class Reconciler:
    """Re-reads one active run's GitHub state per call and applies the diff."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        intake: Intake,
        github: GitHubClient,
        *,
        owner: str = "reconciler",
    ) -> None:
        self.store = store
        self.settings = settings
        self.intake = intake
        self.github = github
        self.owner = owner

    # ------------------------------------------------------------------ driver

    def reconcile_one(
        self, now: datetime | None = None, *, due_before: str | None = None
    ) -> bool:
        """Reconcile the active run longest without a read, if one is due.

        Due means last read before ``due_before``, which defaults to one
        interval ago. Returns whether a run was looked at. A failing read is
        logged and the run is still marked as read, so one broken issue
        cannot pin the reconciler to itself; it is retried after the interval
        like any other.
        """
        if due_before is None:
            now = now or utcnow()
            due_before = (
                now - timedelta(seconds=self.settings.reconcile_interval_seconds)
            ).isoformat()
        run = self.store.reconcile_candidate(self.settings.env, due_before)
        if run is None:
            return False
        run_id = str(run["id"])
        detail: str | None = None
        try:
            applied = self.reconcile_run(run)
            if applied:
                logger.info("reconciled run %s from GitHub: %s", run_id, applied)
        except GitHubError as exc:
            detail = str(exc)
            log = logger.warning if exc.retryable else logger.error
            log("GitHub read for run %s failed: %s", run_id, exc)
        finally:
            with self.store.transaction() as conn:
                self.store.mark_reconciled(conn, run_id, now_iso())
        self.store.heartbeat("reconciler", self.owner, detail)
        return True

    def catch_up(self) -> int:
        """Startup pass: re-read every active run once, whatever its last
        read time. What happened while the service was down is exactly what
        no webhook will tell it. Returns the number of runs looked at."""
        # Everything read before boot is due; everything this pass reads is
        # stamped at or after boot and so is not, which is what ends the loop.
        boot = now_iso()
        looked = 0
        while self.reconcile_one(due_before=boot):
            looked += 1
        return looked

    def reconcile_run(self, run: sqlite3.Row) -> list[str]:
        """Apply what GitHub says about a run's issue and PR. Returns the
        actions the intake accepted, in order."""
        repo = str(run["task_repo"])
        applied = self._reconcile_issue(run, repo)
        current = self.store.get_run(str(run["id"]))
        if current is None or is_terminal(State(str(current["state"]))):
            return applied
        applied += self._reconcile_pull(current, repo)
        if applied:
            with self.store.transaction() as conn:
                self.store.record_event(
                    conn,
                    run_id=str(run["id"]),
                    task_id=int(run["task_id"]),
                    kind="reconcile",
                    reason=",".join(applied),
                    detail={"source": "github_api"},
                )
        return applied

    # ------------------------------------------------------------------- issue

    def _reconcile_issue(self, run: sqlite3.Row, repo: str) -> list[str]:
        number = int(run["issue_number"])
        issue = self.github.get_issue(repo, number)
        label = self.settings.approval_label
        labels = _labels(issue)
        recorded_labels: list[str] = json.loads(str(run["labels"] or "[]"))
        state = str(issue.get("state") or "open")
        recorded_state = str(run["issue_state"] or "open")
        updated_at = str(issue.get("updated_at") or "")
        run_state = State(str(run["state"]))

        action: str | None = None
        sender = ""
        event_id: str | None = None
        with_label = False

        if state == "closed" and recorded_state != "closed":
            action, sender = "closed", _login(issue.get("closed_by"))
        elif state == "open" and recorded_state == "closed":
            action = "reopened"
            sender, event_id = self._actor(repo, number, "reopened")
        elif label not in labels and (
            label in recorded_labels
            or (
                run_state is not State.AWAITING_APPROVAL
                and run["approved_at"]
                and not run["approval_revoked_at"]
            )
        ):
            # Approval withdrawn while nobody was listening.
            action, with_label = "unlabeled", True
            sender, event_id = self._actor(repo, number, "unlabeled", label)
        elif label in labels and (
            run_state is State.AWAITING_APPROVAL or run["approval_revoked_at"]
        ):
            # The label is there and the run does not reflect it. Only the
            # actor who applied it can make that an approval (D-001, D-038).
            sender, event_id = self._actor(repo, number, "labeled", label)
            if not event_id:
                return []
            action, with_label = "labeled", True
        elif updated_at and updated_at != str(run["issue_updated_at"] or ""):
            # Nothing that moves the run; keep the cached snapshot current.
            action = "edited"

        if action is None:
            return []
        payload: dict[str, Any] = {
            "action": action,
            "issue": issue,
            "repository": {"full_name": repo},
            "sender": {"login": sender},
        }
        if with_label:
            payload["label"] = {"name": label}
        revision = event_id or updated_at or "now"
        result = self.intake.handle(
            delivery_id=f"{DELIVERY_PREFIX}:{repo}:issues:{number}:{action}:{revision}",
            event="issues",
            payload=payload,
        )
        return [f"issue.{action}"] if result.accepted else []

    def _actor(
        self, repo: str, number: int, event: str, label: str | None = None
    ) -> tuple[str, str | None]:
        """Who last did ``event`` to the issue, from its event log, with the
        event's id. Empty when the log has no such event, or is too long to
        be read whole: the log is oldest first, so a truncated one could name
        an earlier actor for the latest label, and nobody spends on that."""
        found: dict[str, Any] | None = None
        events = self.github.list_issue_events(repo, number)
        if len(events) >= MAX_ISSUE_EVENTS:
            logger.warning(
                "issue %s#%s has %d+ events; the newest are unread, no actor taken",
                repo,
                number,
                MAX_ISSUE_EVENTS,
            )
            return "", None
        for item in events:
            if str(item.get("event") or "") != event:
                continue
            if (
                label is not None
                and str((item.get("label") or {}).get("name")) != label
            ):
                continue
            found = item
        if found is None:
            return "", None
        return _login(found.get("actor")), str(found.get("id") or "") or None

    # -------------------------------------------------------------------- pull

    def _reconcile_pull(self, run: sqlite3.Row, repo: str) -> list[str]:
        run_id = str(run["id"])
        applied: list[str] = []
        if run["pr_number"] is None:
            pull = self._discover_pull(run, repo)
            if pull is None:
                return applied
            head = str((pull.get("head") or {}).get("sha") or "")
            if self._deliver_pull(repo, _as_open(pull), "opened", head):
                applied.append("pr.opened")
            run_after = self.store.get_run(run_id)
            if run_after is None or run_after["pr_number"] is None:
                return applied
            run = run_after
        else:
            pull = self.github.get_pull(repo, int(run["pr_number"]))
        return applied + self._apply_pull(run, repo, pull)

    def _discover_pull(self, run: sqlite3.Row, repo: str) -> dict[str, Any] | None:
        """The PR on the run's branch, when GitHub has one the run never
        learned about. Only for runs a PR could still attach to (§7)."""
        branch = str(run["branch"] or "")
        if not branch or not can_transition(State(str(run["state"])), State.PR_OPEN):
            return None
        pulls = self.github.list_pulls_for_branch(repo, branch)
        if not pulls:
            return None
        # Prefer one still open; the intake decides whether it belongs.
        return next((p for p in pulls if p.get("state") == "open"), pulls[0])

    def _apply_pull(
        self, run: sqlite3.Row, repo: str, pull: dict[str, Any]
    ) -> list[str]:
        run_id = str(run["id"])
        applied: list[str] = []
        head = str((pull.get("head") or {}).get("sha") or "")
        closed = str(pull.get("state") or "") == "closed"

        # Everything GitHub would have delivered while the PR was open comes
        # first, as it did in reality: the final head, its checks and its
        # reviews are the evidence a merge is judged by, and once the run is
        # terminal it is never read again.
        if head and head != str(run["head_sha"] or ""):
            if self._deliver_pull(repo, _as_open(pull), "synchronize", head):
                applied.append("pr.synchronize")
        elif not closed and bool(pull.get("draft")) != bool(run["pr_draft"]):
            action = _DRAFT_ACTIONS[bool(pull.get("draft"))]
            if self._deliver_pull(repo, pull, action, head):
                applied.append(f"pr.{action}")

        if not closed:
            applied += self._reconcile_protection(run, repo, pull)
        if head:
            applied += self._reconcile_checks(run_id, repo, head)
        applied += self._reconcile_reviews(run_id, repo, int(pull["number"]), pull)

        if closed and str(run["pr_state"] or "") != "closed":
            if self._deliver_pull(repo, pull, "closed", head):
                applied.append("pr.merged" if pull.get("merged") else "pr.closed")
        return applied

    def _deliver_pull(
        self, repo: str, pull: dict[str, Any], action: str, head: str
    ) -> bool:
        payload = {
            "action": action,
            "pull_request": pull,
            "repository": {"full_name": repo},
            "sender": {"login": _login(pull.get("merged_by") or pull.get("user"))},
        }
        number = int(pull["number"])
        # The head alone cannot tell two draft toggles apart; the snapshot's
        # own timestamp can, while a repeat of the same read stays a duplicate.
        revision = f"{head}:{pull.get('updated_at') or '-'}"
        result = self.intake.handle(
            delivery_id=(
                f"{DELIVERY_PREFIX}:{repo}:pull_request:{number}:{action}:{revision}"
            ),
            event="pull_request",
            payload=payload,
        )
        return result.accepted

    # -------------------------------------------------------------- protection

    def _reconcile_protection(
        self, run: sqlite3.Row, repo: str, pull: dict[str, Any]
    ) -> list[str]:
        """Record which status checks the PR's base branch requires.

        An observation beside the check suites, not a gate: "checks passed"
        stays every suite GitHub reported (D-030) until the required contexts
        can be matched to check runs. An unprotected base (404, or no
        ``protection`` because the token lacks push access) requires nothing.
        """
        base = str((pull.get("base") or {}).get("ref") or "")
        if not base:
            return []
        try:
            branch = self.github.get_branch(repo, base)
        except GitHubError as exc:
            if exc.status_code != 404:
                raise
            branch = {}
        required = (branch.get("protection") or {}).get("required_status_checks") or {}
        contexts = sorted(
            {str(c) for c in required.get("contexts") or []}
            | {
                str(c.get("context"))
                for c in required.get("checks") or []
                if c.get("context")
            }
        )
        recorded = sorted(json.loads(str(run["required_checks"] or "[]")))
        if contexts == recorded:
            return []
        with self.store.transaction() as conn:
            self.store.update_run(
                conn, str(run["id"]), required_checks=json.dumps(contexts)
            )
            self.store.record_event(
                conn,
                run_id=str(run["id"]),
                task_id=int(run["task_id"]),
                kind="protection",
                reason="required_checks",
                detail={"base": base, "required_checks": contexts},
            )
        return ["protection.required_checks"]

    # ------------------------------------------------------------------ checks

    def _reconcile_checks(self, run_id: str, repo: str, head: str) -> list[str]:
        known = {
            str(row["suite_id"]): (str(row["status"]), row["conclusion"])
            for row in self.store.checks_for_head(run_id, head)
        }
        applied: list[str] = []
        # The intake judges the head after every suite it stores. Fed one at a
        # time, a passing suite could be the only one it knows and verify the
        # head before the failing or pending one arrives; so those go first.
        suites = sorted(
            self.github.list_check_suites(repo, head), key=_is_successful_suite
        )
        for suite in suites:
            suite_id = str(suite.get("id") or "")
            status = str(suite.get("status") or "queued")
            conclusion = suite.get("conclusion")
            if not suite_id or known.get(suite_id) == (status, conclusion):
                continue
            action = "completed" if status == "completed" else "requested"
            result = self.intake.handle(
                delivery_id=(
                    f"{DELIVERY_PREFIX}:{repo}:check_suite:{suite_id}:{status}"
                    f":{conclusion or '-'}:{suite.get('updated_at') or '-'}"
                ),
                event="check_suite",
                payload={
                    "action": action,
                    "check_suite": {**suite, "head_sha": head},
                    "repository": {"full_name": repo},
                },
            )
            if result.accepted:
                applied.append(f"checks.{conclusion or status}")
        return applied

    # ----------------------------------------------------------------- reviews

    def _reconcile_reviews(
        self, run_id: str, repo: str, number: int, pull: dict[str, Any]
    ) -> list[str]:
        seen_urls = set()
        for event in self.store.events_for_run(run_id):
            if event["kind"] not in {"review", "review_gate"}:
                continue
            detail = json.loads(str(event["detail"] or "{}"))
            if detail.get("url"):
                seen_urls.add(str(detail["url"]))
        reviews = self.github.list_reviews(repo, number)
        # `review_state` is the latest human verdict, so only the newest
        # verdict is worth replaying; an older missed one must not overwrite it.
        verdict_ids = [
            review.get("id")
            for review in reviews
            if not is_review_bot(_login(review.get("user")))
            and str(review.get("state") or "").lower() in HUMAN_REVIEW_OUTCOMES
        ]
        latest_verdict = verdict_ids[-1] if verdict_ids else None
        applied: list[str] = []
        for review in reviews:
            url = str(review.get("html_url") or "")
            # The REST API reports states in upper case; webhooks in lower.
            state = str(review.get("state") or "").lower()
            if not url or url in seen_urls or state == "pending":
                continue
            reviewer = _login(review.get("user"))
            if is_review_bot(reviewer):
                # Devin Review's verdict is a `commented` review whose body
                # carries the count; the row for its head says if it landed.
                commit = str(review.get("commit_id") or "")
                row = self.store.review_for_head(run_id, commit) if commit else None
                if row is not None and row["verdict_at"]:
                    continue
            elif (
                state not in HUMAN_REVIEW_OUTCOMES or review.get("id") != latest_verdict
            ):
                continue
            result = self.intake.handle(
                delivery_id=f"{DELIVERY_PREFIX}:{repo}:review:{review.get('id')}",
                event="pull_request_review",
                payload={
                    "action": "submitted",
                    "review": {**review, "state": state},
                    "pull_request": pull,
                    "repository": {"full_name": repo},
                    "sender": {"login": reviewer},
                },
            )
            if result.accepted:
                applied.append(f"review.{state}")
        return applied
