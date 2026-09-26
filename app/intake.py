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
from app.prompts import extract_marker
from app.states import check_transition, PRE_EXECUTION, State
from app.store import now_iso, Store

SUPPORTED_EVENTS = frozenset({"issues", "pull_request", "pull_request_review", "ping"})
OPEN_ACTIONS = frozenset(
    {"opened", "reopened", "ready_for_review", "synchronize", "edited"}
)
# `synchronize` and `edited` update the record without announcing anything: a
# message per commit is how a channel gets muted.
NOTIFYING_OPEN_ACTIONS = frozenset({"opened", "reopened", "ready_for_review"})


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature-256`` over the raw body.

    The raw body matters: re-serialising the parsed JSON changes the bytes and
    the signature stops matching for reasons that look like an attack.
    """
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", header)


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

        if action in {"opened", "reopened", "edited", "labeled", "unlabeled", "closed"}:
            if action == "closed":
                return self._maybe_cancel(conn, task_id, repo, "issue closed", sender)
            if approved:
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

        self.store.update_run(
            conn,
            run_id,
            approval_revoked_by=sender,
            approval_revoked_at=now_iso(),
        )
        self._notify(
            conn,
            task_id=task_id,
            run_id=run_id,
            kind=Kind.NEEDS_HUMAN,
            reason="approval_revoked",
            revision=run_id,
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
                conn, action, state, fields, task_id, run_id, head_sha
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
        action: str,
        state: State,
        fields: dict[str, Any],
        task_id: int,
        run_id: str,
        head_sha: str,
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
        if state is not State.PR_OPEN:
            check_transition(state, State.PR_OPEN)
            fields["state"] = State.PR_OPEN.value
        self.store.update_run(conn, run_id, **fields)

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
        if run is None:
            return None

        if run["branch"] and branch and str(run["branch"]) != branch:
            return None

        existing_pr = run["pr_number"]
        if existing_pr is not None and int(existing_pr) != int(pull["number"]):
            # First correlated PR wins the lifecycle; a second one is a scope
            # signal for a human, not a second task.
            return None
        return run

    # -------------------------------------------------------------------- reviews

    def _handle_review(
        self, conn: sqlite3.Connection, repo: str, action: str, payload: dict[str, Any]
    ) -> IntakeResult:
        pull = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        run = self._correlate(repo, pull) if pull else None
        if run is None:
            return IntakeResult(False, "review for an untracked PR")
        self.store.update_run(
            conn, str(run["id"]), review_state=str(review.get("state") or "")
        )
        return IntakeResult(
            True,
            f"review {action} recorded",
            task_id=int(run["task_id"]),
            run_id=str(run["id"]),
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
