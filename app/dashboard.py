"""Progress and observability views over the store.

Every figure here is computed for exactly one environment (`live` or `sim`).
The two are never summed, averaged or listed together: a simulated merge in a
live success count is a lie the dashboard would be telling on the pipeline's
behalf.

Outcome vocabulary, kept deliberately narrow:

* **PR opened** — a tracked pull request exists. Checks may be pending.
* **Verified** — the check suite completed successfully on the commit the PR
  currently points at (`checks_state = success` and
  `checks_head_sha = head_sha`). Anything else is not verified, including
  "no checks reported".
* **Merged** — GitHub delivered `closed` with `merged = true`.
* **Blocked / failed** — the run stopped on its own and someone has to act;
  the reason and the next action are always shown together.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Any

from app.states import ACTIVE, is_terminal, State
from app.store import Store, utcnow

ENVIRONMENTS = ("live", "sim")

WORKLOAD_BUCKETS: dict[str, frozenset[State]] = {
    "queued": frozenset({State.AWAITING_APPROVAL, State.QUEUED}),
    "running": frozenset({State.STARTING, State.RUNNING}),
    "awaiting_review": frozenset({State.PR_OPEN, State.AWAITING_REVIEW}),
    "blocked": frozenset({State.SESSION_BLOCKED}),
    "failed": frozenset({State.NO_OUTPUT, State.EXPIRED, State.FAILED}),
}

# Reason -> what a human is expected to do about it. Unknown reasons fall back
# to a generic instruction rather than being hidden.
NEXT_ACTIONS: dict[str, str] = {
    "concurrency": "Wait: held by MAX_CONCURRENT_RUNS. Frees when a run finishes.",
    "daily_cap": "Wait: held by MAX_DAILY_SESSIONS. Resets after 24h or raise cap.",
    "create_retry": "Watch: session create timed out and is being retried.",
    "create_failed": "Check Devin API credentials/quota, then re-apply the label.",
    "session_error": "Open the Devin session, read the error, re-apply the label.",
    "capacity": "Operator: organization is out of Devin capacity or credits.",
    "acu_limit": "Session hit its ACU ceiling without a PR. Narrow the issue.",
    "no_output": "Session finished without a PR. Read its summary, then re-run.",
    "expired": "Run exceeded RUN_MAX_SECONDS. Inspect the session, then re-run.",
    "approval_revoked": "Approval withdrawn; nothing to do unless re-approved.",
    "issue_closed": "Issue closed before execution; nothing to do.",
}
GENERIC_BLOCKED_ACTION = "Open the Devin session and unblock it, or close the issue."


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    parsed = datetime.fromisoformat(str(ts))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _seconds_between(start: str | None, end: str | None) -> float | None:
    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _age_seconds(ts: str | None, now: datetime) -> float | None:
    parsed = _parse(ts)
    return None if parsed is None else (now - parsed).total_seconds()


def is_verified(run: sqlite3.Row) -> bool:
    """Checks passed on the head the PR currently points at, nothing less."""
    return (
        run["pr_url"] is not None
        and run["checks_state"] == "success"
        and run["checks_head_sha"] is not None
        and run["head_sha"] is not None
        and str(run["checks_head_sha"]) == str(run["head_sha"])
    )


def outcome(run: sqlite3.Row) -> str:
    """One word per run for the results section. Precedence: merged > verified >
    pr_opened > blocked/failed > in progress."""
    state = State(str(run["state"]))
    if state is State.MERGED:
        return "merged"
    if state is State.CLOSED_UNMERGED:
        return "closed_unmerged"
    if run["pr_url"]:
        return "verified" if is_verified(run) else "pr_opened"
    if state in WORKLOAD_BUCKETS["blocked"]:
        return "blocked"
    if state in WORKLOAD_BUCKETS["failed"]:
        return "failed"
    if state is State.CANCELLED:
        return "cancelled"
    return "in_progress"


def tests_summary(run: sqlite3.Row) -> dict[str, Any]:
    """Test evidence: what the session said it did, and what CI said.

    The two are kept apart. A session's own claim of having run tests is not a
    check result, and a check result says nothing about which tests exist.
    """
    claimed: dict[str, Any] = {}
    if run["structured_output"]:
        try:
            claimed = json.loads(str(run["structured_output"]))
        except ValueError:
            claimed = {}
    checks = str(run["checks_state"]) if run["checks_state"] else "unknown"
    if run["pr_url"] and run["checks_head_sha"] and run["head_sha"]:
        if str(run["checks_head_sha"]) != str(run["head_sha"]):
            checks = "stale"
    return {
        "checks": checks,
        "checks_head_sha": run["checks_head_sha"],
        "head_sha": run["head_sha"],
        "verified": is_verified(run),
        "tests_added": claimed.get("tests_added"),
        "tests_run": claimed.get("tests_run"),
        "session_outcome": claimed.get("outcome"),
    }


def slack_status(notifications: list[sqlite3.Row]) -> dict[str, Any]:
    """Delivery state of everything queued for a run. Failed beats pending."""
    counts = {"sent": 0, "pending": 0, "failed": 0}
    last_error: str | None = None
    for row in notifications:
        counts[str(row["state"])] = counts.get(str(row["state"]), 0) + 1
        if row["state"] == "failed" and row["last_error"]:
            last_error = str(row["last_error"])
    if counts["failed"]:
        summary = "failed"
    elif counts["pending"]:
        summary = "pending"
    elif counts["sent"]:
        summary = "sent"
    else:
        summary = "none"
    return {"summary": summary, "counts": counts, "last_error": last_error}


def _pr_summary(run: sqlite3.Row) -> dict[str, Any] | None:
    if not run["pr_url"]:
        return None
    return {
        "url": run["pr_url"],
        "number": run["pr_number"],
        "state": run["pr_state"],
        "draft": bool(run["pr_draft"]) if run["pr_draft"] is not None else None,
        "head_sha": run["head_sha"],
        "merged_sha": run["merged_sha"],
        "verified": is_verified(run),
    }


def _next_action(run: sqlite3.Row) -> str | None:
    state = State(str(run["state"]))
    reason = str(run["failure_reason"]) if run["failure_reason"] else None
    if state is State.QUEUED and reason in {"concurrency", "daily_cap", "create_retry"}:
        return NEXT_ACTIONS[reason]
    if state in WORKLOAD_BUCKETS["blocked"] or state in WORKLOAD_BUCKETS["failed"]:
        return NEXT_ACTIONS.get(reason or "", GENERIC_BLOCKED_ACTION)
    return None


def _task_row(
    store: Store, task: sqlite3.Row, run: sqlite3.Row, now: datetime
) -> dict[str, Any]:
    state = State(str(run["state"]))
    started = run["approved_at"] or run["created_at"]
    end = run["updated_at"] if is_terminal(state) else now.isoformat()
    notifications = store.notifications_for_run(str(run["id"]))
    return {
        "task_id": int(task["id"]),
        "run_id": str(run["id"]),
        "repo": str(task["repo"]),
        "issue": {
            "number": int(task["issue_number"]),
            "title": str(task["issue_title"]),
            "state": str(task["issue_state"]),
            "url": f"https://github.com/{task['repo']}/issues/{task['issue_number']}",
        },
        "state": state.value,
        "terminal": is_terminal(state),
        "outcome": outcome(run),
        "failure_reason": run["failure_reason"],
        "next_action": _next_action(run),
        "elapsed_seconds": _seconds_between(started, end),
        "tests": tests_summary(run),
        "pr": _pr_summary(run),
        "session_url": run["session_url"],
        "slack": slack_status(notifications),
        "last_update": run["updated_at"],
        "acus_consumed": run["acus_consumed"],
    }


def _runs_in_env(store: Store, env: str) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    pairs: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    for task in store.list_tasks():
        for run in store.runs_for_task(int(task["id"])):
            if str(run["env"]) == env:
                pairs.append((task, run))
    return pairs


def _review_ready_at(events: list[sqlite3.Row]) -> str | None:
    """First moment the run was verified: entry to `awaiting_review`, or
    `merged` if GitHub confirmed the merge before a check suite reached us."""
    ready = {State.AWAITING_REVIEW.value, State.MERGED.value}
    for event in events:
        if event["kind"] == "state" and event["to_value"] in ready:
            return str(event["at"])
    return None


def _speed(store: Store, runs: list[sqlite3.Row]) -> dict[str, Any]:
    """Approval to review-ready (verified), over runs that got there."""
    samples: list[float] = []
    for run in runs:
        ready_at = _review_ready_at(store.events_for_run(str(run["id"])))
        seconds = _seconds_between(run["approved_at"] or run["created_at"], ready_at)
        if seconds is not None and seconds >= 0:
            samples.append(seconds)
    return {
        "median_seconds_to_review_ready": (
            statistics.median(samples) if samples else None
        ),
        "sample_count": len(samples),
    }


def _throughput(store: Store, env: str) -> list[dict[str, Any]]:
    """Per-day counts of verified and merged transitions. A run verified twice
    (checks re-run after a push) counts once per day it happened."""
    days: dict[str, dict[str, int]] = {}
    for event in store.state_events(env):
        target = event["to_value"]
        if target not in {State.AWAITING_REVIEW.value, State.MERGED.value}:
            continue
        day = str(event["at"])[:10]
        bucket = days.setdefault(day, {"verified": 0, "merged": 0})
        key = "verified" if target == State.AWAITING_REVIEW.value else "merged"
        bucket[key] += 1
    return [{"day": day, **days[day]} for day in sorted(days)]


def _attention(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        reasons: list[str] = []
        if row["next_action"]:
            reasons.append(str(row["failure_reason"] or row["state"]))
        if row["slack"]["summary"] == "failed":
            reasons.append("slack_delivery_failed")
        if row["tests"]["checks"] == "failure" and not row["terminal"]:
            reasons.append("checks_failed")
        if not reasons:
            continue
        action = row["next_action"]
        if action is None and "checks_failed" in reasons:
            action = "CI failed on the current head; review the PR."
        if action is None:
            action = "Slack delivery failed; check the webhook URL and outbox."
        items.append(
            {
                "run_id": row["run_id"],
                "issue": row["issue"],
                "state": row["state"],
                "blocker": ", ".join(reasons),
                "age_seconds": row["elapsed_seconds"],
                "next_action": action,
                "session_url": row["session_url"],
                "pr_url": row["pr"]["url"] if row["pr"] else None,
            }
        )
    items.sort(key=lambda item: -(item["age_seconds"] or 0))
    return items


def _health(store: Store, env: str, now: datetime) -> dict[str, Any]:
    last_poll = store.latest_session_poll_at(env)
    last_delivery = store.latest_delivery_at()
    last_slack = store.latest_notification_sent_at()
    failed = store.failed_notifications(env)
    return {
        "devin": {
            "last_successful_poll": last_poll,
            "age_seconds": _age_seconds(last_poll, now),
        },
        "github": {
            "last_webhook_received": last_delivery,
            "age_seconds": _age_seconds(last_delivery, now),
        },
        "slack": {
            "last_successful_delivery": last_slack,
            "age_seconds": _age_seconds(last_slack, now),
            "failed_count": len(failed),
            "failures": [
                {
                    "outbox_id": int(row["id"]),
                    "run_id": row["run_id"],
                    "kind": row["kind"],
                    "destination": row["destination"],
                    "attempts": int(row["attempts"]),
                    "error": row["last_error"],
                }
                for row in failed[:10]
            ],
        },
    }


def build_dashboard(store: Store, env: str) -> dict[str, Any]:
    """Everything the overview and task table need, for one environment."""
    if env not in ENVIRONMENTS:
        raise ValueError(f"env must be one of {ENVIRONMENTS}, got {env!r}")
    now = utcnow()
    pairs = _runs_in_env(store, env)
    rows = [_task_row(store, task, run, now) for task, run in pairs]
    rows.sort(key=lambda row: str(row["last_update"]), reverse=True)
    runs = [run for _, run in pairs]

    workload = {name: 0 for name in WORKLOAD_BUCKETS}
    for run in runs:
        state = State(str(run["state"]))
        for name, members in WORKLOAD_BUCKETS.items():
            if state in members:
                workload[name] += 1

    results = {"pr_opened": 0, "verified": 0, "merged": 0, "closed_unmerged": 0}
    for run in runs:
        state = State(str(run["state"]))
        if run["pr_url"]:
            results["pr_opened"] += 1
        if is_verified(run):
            results["verified"] += 1
        if state is State.MERGED:
            results["merged"] += 1
        elif state is State.CLOSED_UNMERGED:
            results["closed_unmerged"] += 1

    return {
        "env": env,
        "generated_at": now.isoformat(),
        "definitions": {
            "pr_opened": "A tracked PR exists; checks may be pending.",
            "verified": "Check suite succeeded on the PR's current head commit.",
            "merged": "GitHub reported the PR closed with merged=true.",
            "blocked_failed": "Run stopped on its own; reason and next action shown.",
        },
        "workload": workload,
        "active_runs": sum(1 for run in runs if State(str(run["state"])) in ACTIVE),
        "results": results,
        "speed": _speed(store, runs),
        "throughput": _throughput(store, env),
        "attention": _attention(rows, now),
        "health": _health(store, env, now),
        "tasks": rows,
    }


def build_timeline(store: Store, run_id: str) -> dict[str, Any] | None:
    """One run, end to end: links, every recorded change, evidence, review."""
    run = store.get_run(run_id)
    if run is None:
        return None
    task = store.get_task(int(run["task_id"]))
    if task is None:  # pragma: no cover - foreign key
        return None
    now = utcnow()
    row = _task_row(store, task, run, now)
    events = [
        {
            "at": str(event["at"]),
            "kind": str(event["kind"]),
            "from": event["from_value"],
            "to": event["to_value"],
            "detail": event["detail"],
        }
        for event in store.events_for_run(run_id)
    ]
    notifications = [
        {
            "id": int(n["id"]),
            "kind": n["kind"],
            "reason": n["reason"],
            "destination": n["destination"],
            "state": n["state"],
            "attempts": int(n["attempts"]),
            "last_error": n["last_error"],
            "created_at": n["created_at"],
            "sent_at": n["sent_at"],
        }
        for n in store.notifications_for_run(run_id)
    ]
    review = run["review_state"]
    return {
        **row,
        "env": str(run["env"]),
        "links": {
            "issue": row["issue"]["url"],
            "session": run["session_url"],
            "pr": run["pr_url"],
            "extra_prs": json.loads(str(run["extra_pr_urls"] or "[]")),
        },
        "approval": {
            "approved_by": run["approved_by"],
            "approved_at": run["approved_at"],
            "revoked_by": run["approval_revoked_by"],
            "revoked_at": run["approval_revoked_at"],
        },
        "session": {
            "id": run["session_id"],
            "status": run["session_status"],
            "status_detail": run["session_status_detail"],
            "polled_at": run["session_polled_at"],
            "finished_at": run["session_finished_at"],
            "acus_consumed": run["acus_consumed"],
            "max_acu_limit": run["max_acu_limit"],
        },
        "review": {
            "state": review,
            "outcome": (
                "merged"
                if State(str(run["state"])) is State.MERGED
                else "closed_unmerged"
                if State(str(run["state"])) is State.CLOSED_UNMERGED
                else review or "pending"
            ),
        },
        "error": {
            "reason": run["failure_reason"],
            "next_action": row["next_action"],
        },
        "events": events,
        "notifications": notifications,
    }
