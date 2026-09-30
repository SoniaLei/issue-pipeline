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
"""GitHub webhook intake: verification, deduplication and task transitions.

Everything here runs inside one transaction and makes no external call, so the
endpoint returns promptly and a redelivery cannot produce a second session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.notifications import build_payload, destination_is_operator, fingerprint, Kind
from app.prompts import extract_marker, same_pr_url
from app.review_gate import (
    finding_kind,
    gate_state,
    is_review_bot,
    is_verified,
    parse_summary,
)
from app.states import (
    can_transition,
    check_transition,
    is_terminal,
    PRE_EXECUTION,
    State,
)
from app.store import now_iso, Store

_PR_LIFECYCLE = frozenset(
    {State.PR_OPEN, State.AWAITING_REVIEW, State.MERGED, State.CLOSED_UNMERGED}
)

SUPPORTED_EVENTS = frozenset(
    {
        "issues",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "check_suite",
        "ping",
    }
)

# Check-suite conclusions that mean the head is not verified, whatever any
# other suite says.
FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
)
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
OPEN_ACTIONS = frozenset(
    {"opened", "reopened", "ready_for_review", "synchronize", "edited"}
)
# `synchronize` and `edited` update the record without announcing anything: a
# message per commit is how a channel gets muted.
NOTIFYING_OPEN_ACTIONS = frozenset({"opened", "reopened", "ready_for_review"})
# Human review outcomes worth a line in the PR's thread. A bare comment is
# conversation, not a verdict.
HUMAN_REVIEW_OUTCOMES = frozenset({"approved", "changes_requested"})


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature-256`` over the raw body.

    The raw body matters: re-serialising the parsed JSON changes the bytes and
    the signature stops matching for reasons that look like an attack.
    """
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", header)


def derive_checks_state(suites: list[sqlite3.Row]) -> str:
    """Fold every check suite reported for one head SHA into one verdict.

    Absent suites are ``unknown``, never passed; one failing suite fails the
    head regardless of the others; anything still running is ``pending``. This
    is "every suite GitHub reported", not "the suites branch protection
    requires" — that distinction needs a GitHub API read and is a later uplift.
    """
    if not suites:
        return "unknown"
    conclusions = [suite["conclusion"] for suite in suites]
    if any(c in FAILING_CONCLUSIONS for c in conclusions):
        return "failed"
    if any(suite["status"] != "completed" for suite in suites):
        return "pending"
    if all(c in PASSING_CONCLUSIONS for c in conclusions):
        return "passed"
    return "unknown"


def _primary_is_another_pr(run: sqlite3.Row, pull: dict[str, Any]) -> bool:
    """Whether the run's primary PR, known by number from its webhook or by URL
    from a session poll, is a different PR from `pull`. Only for PRs
    `_correlate` refused: a poll's URL never stops GitHub attaching a PR."""
    if run["pr_number"] is not None:
        return int(run["pr_number"]) != int(pull["number"])
    primary = run["pr_url"]
    url = str(pull.get("html_url") or "")
    return bool(primary) and bool(url) and not same_pr_url(primary, url)


@dataclass(frozen=True)
class IntakeResult:
    accepted: bool
    reason: str
    task_id: int | None = None
    run_id: str | None = None


def _labels(issue: dict[str, Any]) -> list[str]:
    return [str(label["name"]) for label in issue.get("labels") or []]


class Intake:
    """Applies a verified GitHub delivery to durable state."""

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def handle(
        self, *, delivery_id: str, event: str, payload: dict[str, Any]
    ) -> IntakeResult:
        repo = str((payload.get("repository") or {}).get("full_name", ""))
        action = payload.get("action")

        if self.store.delivery_seen(delivery_id):
            return IntakeResult(False, "duplicate delivery")

        if event not in SUPPORTED_EVENTS:
            return self._record_only(
                delivery_id, event, action, repo, payload, "unsupported event"
            )
        if event == "ping":
            return self._record_only(delivery_id, event, action, repo, payload, "ping")
        if not self.settings.repo_allowed(repo):
            return self._record_only(
                delivery_id, event, action, repo, payload, "repository not allowlisted"
            )

        with self.store.transaction() as conn:
            if event == "issues":
                result = self._handle_issue(conn, repo, str(action), payload)
            elif event == "pull_request":
                result = self._handle_pull_request(conn, repo, str(action), payload)
            elif event == "check_suite":
                result = self._handle_check_suite(conn, repo, str(action), payload)
            elif event == "pull_request_review_comment":
                result = self._handle_review_comment(conn, repo, str(action), payload)
            else:
                result = self._handle_review(conn, repo, str(action), payload)

            self.store.record_delivery(
                conn,
                delivery_id=delivery_id,
                event=event,
                action=action if action is None else str(action),
                repo=repo,
                payload=payload,
                accepted=result.accepted,
                reason=result.reason,
            )
        return result

    def _record_only(
        self,
        delivery_id: str,
        event: str,
        action: Any,
        repo: str,
        payload: dict[str, Any],
        reason: str,
    ) -> IntakeResult:
        with self.store.transaction() as conn:
            self.store.record_delivery(
                conn,
                delivery_id=delivery_id,
                event=event,
                action=None if action is None else str(action),
                repo=repo,
                payload=payload,
                accepted=False,
                reason=reason,
            )
        return IntakeResult(False, reason)

    # --------------------------------------------------------------------- issues

    def _handle_issue(
        self, conn: sqlite3.Connection, repo: str, action: str, payload: dict[str, Any]
    ) -> IntakeResult:
        issue = payload.get("issue") or {}
        if not issue:
            return IntakeResult(False, "no issue in payload")
        issue_number = int(issue["number"])
        labels = _labels(issue)
        task_id = self.store.upsert_task(
            conn,
            repo=repo,
            issue_number=issue_number,
            title=str(issue.get("title") or ""),
            issue_state=str(issue.get("state") or "open"),
            labels=labels,
        )

        approved = self.settings.approval_label in labels
        sender = str((payload.get("sender") or {}).get("login", ""))
        added = str((payload.get("label") or {}).get("name", ""))
        # Only an event whose sender is deciding about *this* issue can
        # authorize: applying the label, opening or reopening the issue with it
        # (D-001, D-038). `edited` and unrelated labels carry no such decision.
        authorizing = action in {"opened", "reopened"} or (
            action == "labeled" and added == self.settings.approval_label
        )

        if action in {"opened", "reopened", "edited", "labeled", "unlabeled", "closed"}:
            if action == "closed":
                return self._maybe_cancel(conn, task_id, repo, "issue closed", sender)
            if approved and authorizing:
                # `opened` with the label already present is treated exactly
                # like a label event: the authorization question is the same
                # one, and answering it differently is how a public repository
                # ends up spending money on an unapproved issue.
                return self._approve(conn, task_id, repo, issue, sender, action)
            if action in {"opened", "reopened"}:
                self._ensure_awaiting(conn, task_id)
                return IntakeResult(True, "task awaiting approval", task_id=task_id)
            if action == "unlabeled":
                removed = str((payload.get("label") or {}).get("name", ""))
                if removed == self.settings.approval_label:
                    return self._revoke(conn, task_id, repo, sender)
            return IntakeResult(True, f"issue {action} recorded", task_id=task_id)
        return IntakeResult(True, f"issue {action} recorded", task_id=task_id)

    def _ensure_awaiting(self, conn: sqlite3.Connection, task_id: int) -> str:
        """An issue on its own creates a record, not authorization to spend."""
        if (existing := self.store.active_run_for_task(task_id)) is not None:
            return str(existing["id"])
        return self.store.create_run(
            conn,
            task_id=task_id,
            state=State.AWAITING_APPROVAL,
            env=self.settings.env,
        )

    def _approve(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        repo: str,
        issue: dict[str, Any],
        sender: str,
        action: str,
    ) -> IntakeResult:
        if not self.settings.actor_authorized(sender):
            # The label is present but carries no authority. Recorded, not
            # acted on: on a public repository the label is applied by whoever
            # has triage rights, which is not the same set as whoever may
            # spend money.
            return IntakeResult(
                False,
                f"{sender or 'unknown actor'} is not an authorized approver",
                task_id=task_id,
            )

        if (existing := self.store.active_run_for_task(task_id)) is not None:
            if existing["state"] == State.AWAITING_APPROVAL.value:
                self.store.update_run(
                    conn,
                    str(existing["id"]),
                    state=State.QUEUED.value,
                    approved_by=sender,
                    approved_at=now_iso(),
                    approval_revoked_by=None,
                    approval_revoked_at=None,
                    input_snapshot=json.dumps(issue),
                    max_acu_limit=self.settings.max_acu_limit,
                )
                return IntakeResult(
                    True, "run queued", task_id=task_id, run_id=str(existing["id"])
                )
            if existing["approval_revoked_at"]:
                # Approval was withdrawn mid-flight and an authorized maintainer
                # has put the label back. The session was never stopped, so
                # nothing restarts; the run simply stands approved again.
                self.store.update_run(
                    conn,
                    str(existing["id"]),
                    approved_by=sender,
                    approved_at=now_iso(),
                    approval_revoked_by=None,
                    approval_revoked_at=None,
                )
                self.store.record_event(
                    conn,
                    run_id=str(existing["id"]),
                    task_id=task_id,
                    kind="approval",
                    reason="reinstated",
                    detail={"by": sender, "action": action},
                )
                return IntakeResult(
                    True,
                    "approval reinstated",
                    task_id=task_id,
                    run_id=str(existing["id"]),
                )
            # A redelivered label event, or a second label application while a
            # run is in flight. Neither is a new authorization.
            return IntakeResult(
                True,
                "active run already exists",
                task_id=task_id,
                run_id=str(existing["id"]),
            )

        run_id = self.store.create_run(
            conn,
            task_id=task_id,
            state=State.QUEUED,
            env=self.settings.env,
            approved_by=sender,
            approved_at=now_iso(),
            input_snapshot=issue,
            max_acu_limit=self.settings.max_acu_limit,
        )
        return IntakeResult(
            True, f"run queued from {action}", task_id=task_id, run_id=run_id
        )

    def _revoke(
        self, conn: sqlite3.Connection, task_id: int, repo: str, sender: str
    ) -> IntakeResult:
        """Approval withdrawn.

        Before execution this cancels the run. After it, the session is left
        to finish: the spend is already committed, there is no provider stop
        endpoint, and killing the poll only means paying for work nobody sees.
        A human is told either way.
        """
        run = self.store.active_run_for_task(task_id)
        if run is None:
            return IntakeResult(True, "no active run to revoke", task_id=task_id)
        run_id = str(run["id"])
        state = State(str(run["state"]))
        if state in PRE_EXECUTION:
            self.store.update_run(
                conn,
                run_id,
                state=State.CANCELLED.value,
                approval_revoked_by=sender,
                approval_revoked_at=now_iso(),
                failure_reason="approval_revoked",
            )
            return IntakeResult(True, "run cancelled", task_id=task_id, run_id=run_id)

        revoked_at = now_iso()
        self.store.update_run(
            conn,
            run_id,
            approval_revoked_by=sender,
            approval_revoked_at=revoked_at,
        )
        self._notify(
            conn,
            task_id=task_id,
            run_id=run_id,
            kind=Kind.NEEDS_HUMAN,
            reason="approval_revoked",
            revision=f"{run_id}:{revoked_at}",
        )
        return IntakeResult(
            True,
            "approval revoked, session allowed to finish",
            task_id=task_id,
            run_id=run_id,
        )

    def _maybe_cancel(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        repo: str,
        reason: str,
        sender: str,
    ) -> IntakeResult:
        run = self.store.active_run_for_task(task_id)
        if run is None:
            return IntakeResult(True, reason, task_id=task_id)
        state = State(str(run["state"]))
        if state in PRE_EXECUTION:
            self.store.update_run(
                conn,
                str(run["id"]),
                state=State.CANCELLED.value,
                failure_reason="issue_closed",
            )
            return IntakeResult(
                True, "run cancelled", task_id=task_id, run_id=str(run["id"])
            )
        return IntakeResult(True, reason, task_id=task_id, run_id=str(run["id"]))

    # -------------------------------------------------------------- pull requests

    def _handle_pull_request(
        self, conn: sqlite3.Connection, repo: str, action: str, payload: dict[str, Any]
    ) -> IntakeResult:
        pull = payload.get("pull_request") or {}
        if not pull:
            return IntakeResult(False, "no pull_request in payload")

        run = self._correlate(repo, pull)
        if run is None:
            owner = self._run_with_another_pr(repo, pull)
            if owner is not None and action in NOTIFYING_OPEN_ACTIONS:
                return self._extra_pull_request(conn, owner, pull)
            ended = self._run_that_ended_without_a_pr(repo, pull)
            if ended is not None and action in NOTIFYING_OPEN_ACTIONS:
                return self._late_pull_request(conn, ended, pull)
            # Every other PR on a public repository ends up here. Recorded, in
            # case a run is created later and reconciliation wants it.
            return IntakeResult(False, "PR does not belong to a tracked run")

        task_id = int(run["task_id"])
        run_id = str(run["id"])
        state = State(str(run["state"]))
        head_sha = str((pull.get("head") or {}).get("sha") or "")
        merged = bool(pull.get("merged"))
        draft = bool(pull.get("draft"))

        fields: dict[str, Any] = {
            "pr_number": int(pull["number"]),
            "pr_url": str(pull.get("html_url") or ""),
            "pr_state": str(pull.get("state") or ""),
            "pr_draft": int(draft),
            "head_sha": head_sha,
        }

        if action in OPEN_ACTIONS:
            return self._pull_request_open(
                conn,
                repo,
                action,
                state,
                fields,
                task_id,
                run_id,
                head_sha,
                previous_head=run["head_sha"],
            )
        if action == "closed":
            return self._pull_request_closed(
                conn, state, fields, task_id, run_id, head_sha, pull, merged
            )

        self.store.update_run(conn, run_id, **fields)
        return IntakeResult(
            True, f"PR {action} recorded", task_id=task_id, run_id=run_id
        )

    def _pull_request_open(
        self,
        conn: sqlite3.Connection,
        repo: str,
        action: str,
        state: State,
        fields: dict[str, Any],
        task_id: int,
        run_id: str,
        head_sha: str,
        *,
        previous_head: str | None,
    ) -> IntakeResult:
        if state in {State.MERGED, State.CLOSED_UNMERGED}:
            # A delayed delivery must not resurrect a finished task.
            return IntakeResult(
                True,
                "ignored: task already terminal",
                task_id=task_id,
                run_id=run_id,
            )
        if action == "synchronize":
            # A new head invalidates whatever was known about the old one.
            fields["checks_state"] = None
            fields["checks_head_sha"] = None
            if previous_head and head_sha and previous_head != head_sha:
                stale = self.store.review_for_head(run_id, previous_head)
                if stale is not None:
                    # The review row stays, keyed by the old head; only its
                    # relevance ends. The worker asks for the new head afresh.
                    self.store.record_event(
                        conn,
                        run_id=run_id,
                        task_id=task_id,
                        kind="review_gate",
                        reason="superseded",
                        detail={
                            "head_sha": head_sha,
                            "previous_head": previous_head,
                            "previous_status": stale["status"],
                            "previous_findings": stale["findings"],
                        },
                    )
        if state is not State.PR_OPEN:
            check_transition(state, State.PR_OPEN)
            fields["state"] = State.PR_OPEN.value
        self.store.update_run(conn, run_id, **fields)
        self.store.record_event(
            conn,
            run_id=run_id,
            task_id=task_id,
            kind="pr",
            reason=action,
            detail={
                "number": fields["pr_number"],
                "url": fields["pr_url"],
                "head_sha": head_sha,
                "draft": bool(fields["pr_draft"]),
            },
        )
        if head_sha:
            self._adopt_head_checks(conn, repo, run_id, task_id, head_sha)

        if action in NOTIFYING_OPEN_ACTIONS:
            self._notify(
                conn,
                task_id=task_id,
                run_id=run_id,
                kind=(
                    Kind.READY_FOR_REVIEW
                    if action == "ready_for_review"
                    else Kind.PR_OPENED
                ),
                reason=None,
                revision=(
                    head_sha
                    if action in {"opened", "reopened"}
                    else f"{action}:{head_sha}"
                ),
            )
            run = self.store.get_run(run_id)
            if run is not None:
                extra = json.loads(str(run["extra_pr_urls"]))
                self._notify_scope_prs(
                    conn,
                    task_id,
                    run_id,
                    [url for url in extra if not same_pr_url(url, fields["pr_url"])],
                )
        return IntakeResult(True, f"PR {action}", task_id=task_id, run_id=run_id)

    def _pull_request_closed(
        self,
        conn: sqlite3.Connection,
        state: State,
        fields: dict[str, Any],
        task_id: int,
        run_id: str,
        head_sha: str,
        pull: dict[str, Any],
        merged: bool,
    ) -> IntakeResult:
        target = State.MERGED if merged else State.CLOSED_UNMERGED
        fields["state"] = target.value
        fields["pr_state"] = "closed"
        if merged:
            fields["merged_sha"] = str(pull.get("merge_commit_sha") or "")
        if state is not target:
            check_transition(state, target)
        self.store.update_run(conn, run_id, **fields)
        self.store.record_event(
            conn,
            run_id=run_id,
            task_id=task_id,
            kind="pr",
            reason="merged" if merged else "closed",
            detail={
                "number": fields["pr_number"],
                "url": fields["pr_url"],
                "head_sha": head_sha,
                "merged_sha": fields.get("merged_sha"),
                "merged_by": str((pull.get("merged_by") or {}).get("login") or ""),
            },
        )
        self._notify(
            conn,
            task_id=task_id,
            run_id=run_id,
            kind=Kind.PR_MERGED if merged else Kind.PR_CLOSED,
            reason=None,
            revision=str(fields.get("merged_sha") or head_sha),
        )
        return IntakeResult(
            True, f"PR closed (merged={merged})", task_id=task_id, run_id=run_id
        )

    def _correlate(self, repo: str, pull: dict[str, Any]) -> sqlite3.Row | None:
        """Attach a PR to a run, or refuse to.

        Three independent conditions, and all of them are required. The
        repository must be the one the run was authorized for; the head branch
        must be the branch the run was told to use, or the body must carry the
        run marker; and the run must still be eligible to take a PR. The
        marker alone is not enough — it is public text in a public repository,
        and a stranger can paste it into their own pull request.
        """
        run = self._lookup_run(repo, pull)
        if run is None:
            return None

        branch = str((pull.get("head") or {}).get("ref") or "")
        if run["branch"] and branch and str(run["branch"]) != branch:
            return None

        existing_pr = run["pr_number"]
        if existing_pr is not None and int(existing_pr) != int(pull["number"]):
            # First correlated PR wins the lifecycle; a second one is a scope
            # signal for a human, not a second task.
            return None

        state = State(str(run["state"]))
        if state not in _PR_LIFECYCLE and not can_transition(state, State.PR_OPEN):
            return None
        return run

    def _lookup_run(self, repo: str, pull: dict[str, Any]) -> sqlite3.Row | None:
        """The run a same-repository PR names by branch or marker, if any."""
        head = pull.get("head") or {}
        head_repo = str((head.get("repo") or {}).get("full_name") or "")
        if head_repo and head_repo.lower() != repo.lower():
            # A fork. Devin pushes to a branch on the repository itself, so a
            # PR from elsewhere is somebody else's contribution.
            return None
        branch = str(head.get("ref") or "")
        run = self.store.find_run_by_branch(repo, branch) if branch else None
        if run is None:
            marker = extract_marker(pull.get("body"))
            if marker:
                run = self.store.find_run_in_repo(repo, marker)
        return run

    def _run_with_another_pr(
        self, repo: str, pull: dict[str, Any]
    ) -> sqlite3.Row | None:
        """The run this PR names when that run already owns a different PR."""
        run = self._lookup_run(repo, pull)
        if run is None or not _primary_is_another_pr(run, pull):
            return None
        return run

    def _extra_pull_request(
        self, conn: sqlite3.Connection, run: sqlite3.Row, pull: dict[str, Any]
    ) -> IntakeResult:
        """Record a run's second PR and tell a human, once per PR.

        Until the primary PR's own webhook has queued the run's "PR opened"
        anchor, the alert waits for it (see `_notify_scope_prs`), so it lands in
        that PR's thread rather than top-level."""
        run_id = str(run["id"])
        task_id = int(run["task_id"])
        url = str(pull.get("html_url") or "")
        extra = json.loads(str(run["extra_pr_urls"] or "[]"))
        if url and url not in extra:
            extra.append(url)
            self.store.update_run(conn, run_id, extra_pr_urls=json.dumps(extra))
            self.store.record_event(
                conn,
                run_id=run_id,
                task_id=task_id,
                kind="pr",
                reason="extra",
                detail={"number": int(pull["number"]), "url": url},
            )
        if run["pr_number"] is not None:
            self._notify_scope_prs(conn, task_id, run_id, [url])
        return IntakeResult(
            True, "extra PR recorded as a scope signal", task_id=task_id, run_id=run_id
        )

    def _run_that_ended_without_a_pr(
        self, repo: str, pull: dict[str, Any]
    ) -> sqlite3.Row | None:
        """The `no_output` run whose recorded branch this PR is on, if any.

        The marker is public text, so it cannot attribute a PR to a session on
        its own; only the branch the run recorded before the session started
        can."""
        run = self._lookup_run(repo, pull)
        if run is None or State(str(run["state"])) is not State.NO_OUTPUT:
            return None
        branch = str((pull.get("head") or {}).get("ref") or "")
        if not run["branch"] or str(run["branch"]) != branch:
            return None
        return run

    def _late_pull_request(
        self, conn: sqlite3.Connection, run: sqlite3.Row, pull: dict[str, Any]
    ) -> IntakeResult:
        """Tell a human, once per PR, that a session opened a PR after its run
        ended as `no_output`. The run stays terminal and the PR is not
        attached; re-running is a new run (D-030)."""
        run_id = str(run["id"])
        task_id = int(run["task_id"])
        url = str(pull.get("html_url") or "")
        if self._notify(
            conn,
            task_id=task_id,
            run_id=run_id,
            kind=Kind.NEEDS_HUMAN,
            reason="late_pr",
            revision=f"late:{url}",
            detail=url,
        ):
            self.store.record_event(
                conn,
                run_id=run_id,
                task_id=task_id,
                kind="pr",
                reason="late",
                detail={"number": int(pull["number"]), "url": url},
            )
        return IntakeResult(
            False,
            "late PR for a run that ended without one; human alerted",
            task_id=task_id,
            run_id=run_id,
        )

    def _notify_scope_prs(
        self, conn: sqlite3.Connection, task_id: int, run_id: str, urls: list[str]
    ) -> None:
        for url in urls:
            self._notify(
                conn,
                task_id=task_id,
                run_id=run_id,
                kind=Kind.NEEDS_HUMAN,
                reason="scope",
                revision=f"scope:{url}",
                detail=url,
            )

    # -------------------------------------------------------------------- reviews

    def _handle_review(
        self, conn: sqlite3.Connection, repo: str, action: str, payload: dict[str, Any]
    ) -> IntakeResult:
        pull = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        run = self._correlate(repo, pull) if pull else None
        if run is None:
            return IntakeResult(False, "review for an untracked PR")
        reviewer = str((review.get("user") or {}).get("login") or "")
        if is_review_bot(reviewer):
            # Devin Review's summary: the verdict of the review gate, tied to
            # the commit it reviewed. Not a human review, so it never touches
            # `review_state`.
            return self._handle_bot_review(conn, run, action, review)
        review_state = str(review.get("state") or "")
        self.store.update_run(conn, str(run["id"]), review_state=review_state)
        self.store.record_event(
            conn,
            run_id=str(run["id"]),
            task_id=int(run["task_id"]),
            kind="review",
            reason=review_state,
            detail={
                "reviewer": reviewer,
                "url": review.get("html_url"),
                "commit": review.get("commit_id"),
            },
        )
        if action == "submitted" and review_state in HUMAN_REVIEW_OUTCOMES:
            self._notify(
                conn,
                task_id=int(run["task_id"]),
                run_id=str(run["id"]),
                kind=Kind.HUMAN_REVIEW,
                reason=review_state,
                revision=str(review.get("id") or review.get("commit_id") or ""),
                detail=f"by {reviewer}" if reviewer else None,
            )
        return IntakeResult(
            True,
            f"review {action} recorded",
            task_id=int(run["task_id"]),
            run_id=str(run["id"]),
        )

    def _handle_bot_review(
        self,
        conn: sqlite3.Connection,
        run: sqlite3.Row,
        action: str,
        review: dict[str, Any],
    ) -> IntakeResult:
        run_id = str(run["id"])
        task_id = int(run["task_id"])
        commit = str(review.get("commit_id") or "")
        findings = parse_summary(review.get("body"))
        if not commit or findings is None or action != "submitted":
            return IntakeResult(False, "Devin Review comment without a verdict")
        self.store.upsert_review(
            conn,
            run_id=run_id,
            head_sha=commit,
            env=str(run["env"]),
            pr_url=str(run["pr_url"] or ""),
            findings=findings,
            review_url=review.get("html_url"),
            verdict_at=now_iso(),
        )
        self.store.record_event(
            conn,
            run_id=run_id,
            task_id=task_id,
            kind="review_gate",
            reason="clear" if findings == 0 else "findings",
            detail={
                "head_sha": commit,
                "findings": findings,
                "url": review.get("html_url"),
                "current_head": commit == run["head_sha"],
            },
        )
        if commit == run["head_sha"]:
            if findings > 0 and not is_terminal(State(str(run["state"]))):
                self._notify(
                    conn,
                    task_id=task_id,
                    run_id=run_id,
                    kind=Kind.REVIEW_FINDINGS,
                    reason=None,
                    revision=commit,
                    detail=f"{findings} finding{'s' if findings != 1 else ''}"
                    f" on {commit[:10]}",
                )
            self._record_verified_if_due(conn, run_id, task_id, commit)
        return IntakeResult(
            True, "Devin Review verdict recorded", task_id=task_id, run_id=run_id
        )

    def _handle_review_comment(
        self, conn: sqlite3.Connection, repo: str, action: str, payload: dict[str, Any]
    ) -> IntakeResult:
        """Count Devin Review's inline findings by kind (bug, security, flag)
        for the commit they were left on. Human review comments are not
        recorded."""
        comment = payload.get("comment") or {}
        pull = payload.get("pull_request") or {}
        kind = finding_kind(comment.get("body"))
        login = (comment.get("user") or {}).get("login")
        if not is_review_bot(login) or kind is None:
            return IntakeResult(False, "not a Devin Review finding")
        if action != "created":
            return IntakeResult(False, f"finding comment {action} ignored")
        run = self._correlate(repo, pull) if pull else None
        if run is None:
            return IntakeResult(False, "finding for an untracked PR")
        commit = str(comment.get("commit_id") or "")
        if not commit:
            return IntakeResult(False, "finding without a commit")
        run_id = str(run["id"])
        row = self.store.review_for_head(run_id, commit)
        by_kind: dict[str, int] = (
            json.loads(str(row["findings_by_kind"])) if row is not None else {}
        )
        by_kind[kind] = by_kind.get(kind, 0) + 1
        self.store.upsert_review(
            conn,
            run_id=run_id,
            head_sha=commit,
            env=str(run["env"]),
            pr_url=str(run["pr_url"] or ""),
            findings_by_kind=json.dumps(by_kind, sort_keys=True),
        )
        return IntakeResult(
            True,
            f"Devin Review {kind} finding recorded",
            task_id=int(run["task_id"]),
            run_id=run_id,
        )

    def _record_verified_if_due(
        self, conn: sqlite3.Connection, run_id: str, task_id: int, head_sha: str
    ) -> None:
        """Record *verified* for a head once, when GitHub checks have passed
        on it and the review gate is satisfied for it (D-033). Called from
        every path that can complete either half."""
        run = self.store.get_run(run_id)
        if run is None or run["head_sha"] != head_sha:
            return
        mode = self.settings.review_gate_mode
        review = self.store.review_for_head(run_id, head_sha)
        if not is_verified(run, review, mode):
            return
        for event in self.store.events_for_run(run_id):
            if event["kind"] != "verified":
                continue
            detail = json.loads(str(event["detail"] or "{}"))
            if detail.get("head_sha") == head_sha:
                return
        self.store.record_event(
            conn,
            run_id=run_id,
            task_id=task_id,
            kind="verified",
            detail={
                "head_sha": head_sha,
                "review_gate": gate_state(review, mode),
                "mode": mode,
            },
        )
        if is_terminal(State(str(run["state"]))):
            # The PR has left review; "ready for human review" would be false.
            return
        self._notify(
            conn,
            task_id=task_id,
            run_id=run_id,
            kind=Kind.VERIFIED,
            reason=None,
            revision=head_sha,
        )

    # --------------------------------------------------------------------- checks

    def _handle_check_suite(
        self, conn: sqlite3.Connection, repo: str, action: str, payload: dict[str, Any]
    ) -> IntakeResult:
        """Observe check suites for a tracked head.

        The derived verdict feeds the report and the dashboard and moves no
        state. The only message it produces is on the head's transition into
        *failed* (D-036): one per head, never one per suite, and never for
        pending or passed — passing is announced by *verified*, which also
        needs the review gate.
        """
        suite = payload.get("check_suite") or {}
        head_sha = str(suite.get("head_sha") or "")
        if not suite or not head_sha:
            return IntakeResult(False, "no check_suite in payload")

        app = (suite.get("app") or {}).get("slug") or (suite.get("app") or {}).get(
            "name"
        )
        # Kept per head regardless of whether a run tracks it yet: the suites
        # for a commit are usually requested before the PR carrying it opens.
        self.store.record_head_check_suite(
            conn,
            repo=repo,
            head_sha=head_sha,
            suite_id=str(suite.get("id") or ""),
            app=str(app) if app else None,
            status=str(suite.get("status") or "queued"),
            conclusion=suite.get("conclusion"),
            url=suite.get("html_url") or suite.get("url"),
        )

        runs = self.store.runs_with_head(repo, head_sha)
        if not runs:
            return IntakeResult(False, "check suite for an untracked head")

        for run in runs:
            run_id = str(run["id"])
            before = derive_checks_state(self.store.checks_for_head(run_id, head_sha))
            self.store.record_check_suite(
                conn,
                run_id=run_id,
                head_sha=head_sha,
                suite_id=str(suite.get("id") or ""),
                app=str(app) if app else None,
                status=str(suite.get("status") or "queued"),
                conclusion=suite.get("conclusion"),
                url=suite.get("html_url") or suite.get("url"),
            )
            after = derive_checks_state(self.store.checks_for_head(run_id, head_sha))
            self.store.update_run(
                conn, run_id, checks_state=after, checks_head_sha=head_sha
            )
            self.store.record_event(
                conn,
                run_id=run_id,
                task_id=int(run["task_id"]),
                kind="checks",
                reason=after,
                detail={
                    "app": app,
                    "status": suite.get("status"),
                    "conclusion": suite.get("conclusion"),
                    "head_sha": head_sha,
                },
            )
            if after == "passed" and before != "passed":
                self._record_verified_if_due(
                    conn, run_id, int(run["task_id"]), head_sha
                )
            elif after == "failed" and before != "failed":
                self._notify_checks_failed(conn, run, head_sha)
        return IntakeResult(
            True,
            f"check suite {action} recorded",
            task_id=int(runs[0]["task_id"]),
            run_id=str(runs[0]["id"]),
        )

    def _adopt_head_checks(
        self,
        conn: sqlite3.Connection,
        repo: str,
        run_id: str,
        task_id: int,
        head_sha: str,
    ) -> None:
        if self.store.checks_for_head(run_id, head_sha):
            # Suites for this head already flow to the run directly.
            return
        adopted = self.store.adopt_head_checks(
            conn, run_id=run_id, repo=repo, head_sha=head_sha
        )
        if adopted == 0:
            return
        after = derive_checks_state(self.store.checks_for_head(run_id, head_sha))
        self.store.update_run(
            conn, run_id, checks_state=after, checks_head_sha=head_sha
        )
        self.store.record_event(
            conn,
            run_id=run_id,
            task_id=task_id,
            kind="checks",
            reason=after,
            detail={"head_sha": head_sha, "suites_adopted": adopted},
        )
        if after == "passed":
            self._record_verified_if_due(conn, run_id, task_id, head_sha)
        elif after == "failed":
            run = self.store.get_run(run_id)
            if run is not None:
                self._notify_checks_failed(conn, run, head_sha)

    def _notify_checks_failed(
        self, conn: sqlite3.Connection, run: sqlite3.Row, head_sha: str
    ) -> None:
        if run["head_sha"] != head_sha or is_terminal(State(str(run["state"]))):
            # A failure on a superseded head or a closed PR is history, not news.
            return
        self._notify(
            conn,
            task_id=int(run["task_id"]),
            run_id=str(run["id"]),
            kind=Kind.CHECKS_FAILED,
            reason=None,
            revision=head_sha,
            detail=f"on {head_sha[:10]}",
        )

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
    ) -> bool:
        task = self.store.get_task(task_id)
        if task is None:
            return False
        run = self.store.get_run(run_id) if run_id else None
        repo = str(task["repo"])
        destination = self.settings.destination_for(
            repo, operator=destination_is_operator(reason)
        )
        payload = build_payload(
            kind=kind, task=task, run=run, reason=reason, detail=detail
        )
        return self.store.enqueue_notification(
            conn,
            task_id=task_id,
            run_id=run_id,
            kind=kind.value,
            reason=reason,
            fingerprint=fingerprint(
                task_id=task_id, kind=kind, destination=destination, revision=revision
            ),
            destination=destination,
            payload=payload,
        )
