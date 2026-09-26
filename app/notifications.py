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
"""Notification vocabulary, fingerprints and message formatting."""

from __future__ import annotations

import hashlib
import sqlite3
from enum import Enum
from typing import Any


class Kind(str, Enum):
    PR_OPENED = "pr_opened"
    READY_FOR_REVIEW = "ready_for_review"
    VERIFIED = "verified"
    NEEDS_HUMAN = "needs_human"
    PR_MERGED = "pr_merged"
    PR_CLOSED = "pr_closed"


# Reasons carried by needs_human. `capacity` is owned by whoever runs the
# pipeline rather than by the maintainer who filed the issue, which is why it
# routes elsewhere.
OPERATOR_REASONS = frozenset({"capacity", "session_error", "uncertain_create"})


def fingerprint(
    *, task_id: int, kind: Kind, destination: str, revision: str | None
) -> str:
    """Identity of a logical notification.

    The revision component is the part that matters: without it a new commit
    is silently swallowed as a duplicate, and with the wrong thing in it every
    poll produces a message.
    """
    raw = f"{task_id}|{kind.value}|{destination}|{revision or '-'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def escape(text: str | None) -> str:
    """Neutralise user-controlled text for Slack.

    Two separate problems: Slack's own control characters, and channel-wide
    mentions. Issue titles on a public repository are written by strangers,
    and `@channel` in a bug report should not wake anyone up.
    """
    if not text:
        return ""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for mention in ("@channel", "@here", "@everyone"):
        escaped = escaped.replace(mention, mention.replace("@", "@\u200b"))
    return escaped


def _link(url: str | None, label: str) -> str:
    if not url:
        return label
    return f"<{url}|{escape(label)}>"


def _issue_url(repo: str, issue_number: int) -> str:
    return f"https://github.com/{repo}/issues/{issue_number}"


def build_payload(
    *,
    kind: Kind,
    task: sqlite3.Row,
    run: sqlite3.Row | None,
    reason: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Render a Slack message.

    Every message names the next human action, because a notification that
    tells you something happened but not what to do with it is noise on the
    second day.
    """
    repo = str(task["repo"])
    issue_number = int(task["issue_number"])
    title = escape(str(task["issue_title"]))

    pr_url = str(run["pr_url"]) if run and run["pr_url"] else None
    pr_number = run["pr_number"] if run else None
    session_url = str(run["session_url"]) if run and run["session_url"] else None
    run_id = str(run["id"]) if run else None
    draft = bool(run["pr_draft"]) if run and run["pr_draft"] is not None else False
    checks = str(run["checks_state"]) if run and run["checks_state"] else "unknown"
    pr_state = _pr_state(kind, run, draft)

    headline, next_action = _headline(kind, title, reason, detail, draft)

    lines = [
        f"*{headline}*",
        f"Repository: `{escape(repo)}`",
        f"Issue: {_link(_issue_url(repo, issue_number), f'#{issue_number} {title}')}",
    ]
    if pr_url:
        lines.append(f"PR: {_link(pr_url, f'#{pr_number}')}")
        lines.append(f"State: {pr_state} | Checks: {checks}")
    if session_url:
        lines.append(f"Devin session: {_link(session_url, 'open session')}")
    if run_id:
        lines.append(f"Run: `{run_id}`")
    if detail:
        lines.append(f"Detail: {escape(detail)}")
    lines.append(f"Next action: {next_action}")

    text = f"{headline} — {repo}#{issue_number}"
    return {
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        ],
    }


def _pr_state(kind: Kind, run: sqlite3.Row | None, draft: bool) -> str:
    if kind is Kind.PR_MERGED:
        return "merged"
    if kind is Kind.PR_CLOSED:
        return "closed unmerged"
    if run is not None and run["pr_state"]:
        return str(run["pr_state"])
    return "draft" if draft else "open"


def _headline(
    kind: Kind, title: str, reason: str | None, detail: str | None, draft: bool
) -> tuple[str, str]:
    if kind is Kind.PR_OPENED:
        return (
            f"PR opened: {title}",
            "wait for checks" if draft else "review the change",
        )
    if kind is Kind.READY_FOR_REVIEW:
        # Draft status is an intent, not a verification. Saying anything about
        # CI here would be a lie the channel would learn to trust.
        return f"PR marked ready for review: {title}", "review the change"
    if kind is Kind.VERIFIED:
        return f"PR verified on latest head: {title}", "review and merge"
    if kind is Kind.PR_MERGED:
        return f"PR merged: {title}", "none — merged, not deployed"
    if kind is Kind.PR_CLOSED:
        return f"PR closed without merging: {title}", "decide whether to re-run"
    return _needs_human_headline(title, reason)


def _needs_human_headline(title: str, reason: str | None) -> tuple[str, str]:
    mapping = {
        "waiting_for_user": (
            "Session is waiting for a human",
            "open the session and reply",
        ),
        "approval": (
            "Session is waiting for approval to act",
            "open the session and approve or decline",
        ),
        "inactivity": ("Session suspended after inactivity", "resume the session"),
        "user_request": ("Session suspended", "resume the session or close the run"),
        "no_output": (
            "Session finished without opening a PR",
            "read the session and decide whether to re-run",
        ),
        "expired": ("Run exceeded its time limit", "inspect the session"),
        "capacity": (
            "Devin capacity or credit limit reached",
            "operator: top up or raise the limit — this is not a failed fix",
        ),
        "session_error": ("Session errored", "inspect the session"),
        "acu_limit": (
            "Run hit its ACU ceiling",
            "raise the ceiling and re-run deliberately — an identical retry"
            " fails identically",
        ),
        "uncertain_create": (
            "Session creation could not be confirmed",
            "operator: check for an orphaned session before re-running",
        ),
        "approval_revoked": (
            "Approval was withdrawn while the run was in flight",
            "the session is being allowed to finish — review with that in mind",
        ),
        "scope": (
            "Session opened more than one PR",
            "check whether the change outgrew its scope",
        ),
    }
    headline, action = mapping.get(
        reason or "", ("Run needs attention", "inspect the run")
    )
    return f"{headline}: {title}", action


def destination_is_operator(reason: str | None) -> bool:
    return reason in OPERATOR_REASONS
