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
"""The operator dashboard: one environment at a time, definitions attached.

Everything here is derived from the store; nothing is written. Two rules the
numbers depend on:

* A report is built for exactly one environment. Simulated runs and live runs
  never appear in the same total, because a success rate that quietly includes
  fixtures is a number nobody should trust.
* Outcomes are defined by GitHub evidence, not by what Devin says. A PR is
  *opened* when GitHub has it, *verified* when every check suite GitHub
  reported for the PR's current head has passed, and *merged* when GitHub says
  ``merged=true``. A finished session is none of these.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.notifications import needs_human_text
from app.states import is_terminal, State
from app.store import Store, utcnow

DEFINITIONS: dict[str, str] = {
    "pr_opened": "A pull request exists on GitHub for the run. Checks may be pending.",
    "verified": (
        "Every check suite GitHub reported for the PR's current head commit "
        "completed successfully. A new commit resets this to unknown."
    ),
    "merged": "GitHub reported the pull request closed with merged=true.",
    "blocked": (
        "The run stopped making progress on its own: the session is waiting on "
        "a human, finished without a PR, or ran out of time."
    ),
    "failed": (
        "The run ended without a PR: session error, ACU ceiling or create failure."
    ),
    "review_ready": (
        "Time from maintainer approval to the first moment the PR was verified."
    ),
    "environments": (
        "Live and simulated runs are reported separately and never combined."
    ),
}

# Application state -> the bucket the overview counts it under. `pr_open` is
# split at report time into pr_open / awaiting_review by verification.
_BUCKET: dict[State, str] = {
    State.AWAITING_APPROVAL: "awaiting_approval",
    State.QUEUED: "queued",
    State.STARTING: "running",
    State.RUNNING: "running",
    State.SESSION_BLOCKED: "blocked",
    State.PR_OPEN: "pr_open",
    State.AWAITING_REVIEW: "awaiting_review",
    State.NO_OUTPUT: "blocked",
    State.EXPIRED: "blocked",
    State.FAILED: "failed",
    State.CANCELLED: "cancelled",
    State.MERGED: "merged",
    State.CLOSED_UNMERGED: "closed_unmerged",
}

WORKLOAD_BUCKETS = (
    "awaiting_approval",
    "queued",
    "running",
    "pr_open",
    "awaiting_review",
    "blocked",
    "failed",
)

THROUGHPUT_DAYS = 14


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _seconds_between(start: Any, end: Any) -> float | None:
    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def _json(value: Any) -> Any:
    if not value:
        return None
    return json.loads(str(value))


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def is_verified(run: sqlite3.Row) -> bool:
    """Checks passed on the head GitHub currently reports for the PR."""
    return bool(
        run["pr_number"] is not None
        and run["head_sha"]
        and run["checks_state"] == "passed"
        and run["checks_head_sha"] == run["head_sha"]
    )


def bucket_for(run: sqlite3.Row) -> str:
    state = State(str(run["state"]))
    if state is State.PR_OPEN and is_verified(run):
        return "awaiting_review"
    return _BUCKET[state]


def _checks_view(store: Store, run: sqlite3.Row) -> dict[str, Any]:
    head = run["head_sha"]
    current = bool(head) and run["checks_head_sha"] == head
    state = str(run["checks_state"]) if current and run["checks_state"] else "unknown"
    suites = (
        [
            {
                "app": row["app"],
                "status": row["status"],
                "conclusion": row["conclusion"],
                "url": row["url"],
            }
            for row in store.checks_for_head(str(run["id"]), str(head))
        ]
        if head
        else []
    )
    return {"state": state, "head_sha": head, "suites": suites}


def _tests_view(store: Store, run: sqlite3.Row) -> dict[str, Any]:
    """Test evidence, from two independent sources kept apart.

    The session's structured output is Devin's own account of what it did;
    the check state is GitHub's. The table shows both because they can and
    do disagree.
    """
    output = _json(run["structured_output"]) or {}
    tests_added = output.get("tests_added")
    return {
        "regression_test": (
            "added"
            if tests_added is True
            else "not added"
            if tests_added is False
            else "not reported"
        ),
        "tests_run": output.get("tests_run"),
        "session_outcome": output.get("outcome"),
        "checks": _checks_view(store, run),
    }


def _slack_view(rows: list[sqlite3.Row]) -> dict[str, Any]:
    counts = {"sent": 0, "pending": 0, "failed": 0}
    last_error = None
    last_sent_at = None
    for row in rows:
        counts[str(row["state"])] = counts.get(str(row["state"]), 0) + 1
        if row["last_error"] and row["state"] != "sent":
            last_error = row["last_error"]
        if row["sent_at"] and (last_sent_at is None or row["sent_at"] > last_sent_at):
            last_sent_at = row["sent_at"]
    if counts["failed"]:
        status = "failed"
    elif counts["pending"]:
        status = "pending"
    elif counts["sent"]:
        status = "sent"
    else:
        status = "none"
    return {
        "status": status,
        **counts,
        "last_error": last_error,
        "last_sent_at": last_sent_at,
    }


def _first_event_at(events: list[sqlite3.Row], kind: str) -> str | None:
    for event in events:
        if event["kind"] == kind:
            return str(event["at"])
    return None


def _entered_state_at(events: list[sqlite3.Row], state: State) -> str | None:
    at = None
    for event in events:
        if event["kind"] == "state" and event["to_state"] == state.value:
            at = str(event["at"])
    return at


def _run_timing(
    run: sqlite3.Row, events: list[sqlite3.Row], now: datetime
) -> dict[str, Any]:
    state = State(str(run["state"]))
    started = run["approved_at"] or run["created_at"]
    ended: str | None = None
    if is_terminal(state):
        ended = _entered_state_at(events, state) or run["updated_at"]
    end = ended or now.isoformat()
    return {
        "started_at": started,
        "ended_at": ended,
        "elapsed_seconds": _seconds_between(started, end),
        "pr_opened_seconds": _seconds_between(
            run["approved_at"], _first_event_at(events, "pr")
        ),
        "review_ready_seconds": _seconds_between(
            run["approved_at"], _first_event_at(events, "verified")
        ),
    }


def _attention(
    run: sqlite3.Row, bucket: str, timing: dict[str, Any], slack: dict[str, Any]
) -> dict[str, Any] | None:
    """Why a human should look, and what they should do."""
    reason: str | None = None
    blocker: str | None = None
    action: str | None = None
    if bucket in {"blocked", "failed"}:
        reason = str(run["failure_reason"] or "")
        blocker, action = needs_human_text(reason or None)
    elif run["approval_revoked_at"] and not is_terminal(State(str(run["state"]))):
        reason = "approval_revoked"
        blocker, action = needs_human_text(reason)
    elif bucket == "queued" and run["failure_reason"] in {"concurrency", "daily_cap"}:
        reason = str(run["failure_reason"])
        blocker = f"Held in queue by the {reason.replace('_', ' ')} limit"
        action = "nothing yet — starts when a slot frees; raise the limit if urgent"
    elif bucket == "pr_open" and run["checks_state"] == "failed":
        reason = "checks_failed"
        blocker = "Checks failed on the current PR head"
        action = "read the failing check and reply in the session or fix by hand"
    elif slack["status"] == "failed":
        reason = "slack_delivery"
        blocker = "A Slack notification could not be delivered"
        action = "operator: check the webhook URL and destination mapping"
    if blocker is None:
        return None
    return {
        "reason": reason,
        "blocker": blocker,
        "next_action": action,
        "age_seconds": timing["elapsed_seconds"],
    }


def _run_view(store: Store, run: sqlite3.Row, now: datetime) -> dict[str, Any]:
    events = store.events_for_run(str(run["id"]))
    bucket = bucket_for(run)
    timing = _run_timing(run, events, now)
    slack = _slack_view(
        [
            row
            for row in store.notifications_for_task(int(run["task_id"]))
            if row["run_id"] == run["id"]
        ]
    )
    repo = str(run["task_repo"])
    return {
        "run_id": str(run["id"]),
        "task_id": int(run["task_id"]),
        "env": str(run["env"]),
        "repo": repo,
        "issue": {
            "number": int(run["issue_number"]),
            "title": str(run["issue_title"]),
            "state": str(run["issue_state"]),
            "url": f"https://github.com/{repo}/issues/{int(run['issue_number'])}",
        },
        "state": str(run["state"]),
        "bucket": bucket,
        "verified": is_verified(run),
        "failure_reason": run["failure_reason"],
        "approved_by": run["approved_by"],
        "approval_revoked_by": run["approval_revoked_by"],
        "session": {
            "id": run["session_id"],
            "url": run["session_url"],
            "status": run["session_status"],
            "status_detail": run["session_status_detail"],
            "polled_at": run["session_polled_at"],
            "acus_consumed": run["acus_consumed"],
            "max_acu_limit": run["max_acu_limit"],
        },
        "pr": {
            "number": run["pr_number"],
            "url": run["pr_url"],
            "state": run["pr_state"],
            "draft": bool(run["pr_draft"]) if run["pr_draft"] is not None else None,
            "head_sha": run["head_sha"],
            "branch": run["branch"],
            "merged_sha": run["merged_sha"],
            "review_state": run["review_state"],
        },
        "tests": _tests_view(store, run),
        "slack": slack,
        "timing": timing,
        "attention": _attention(run, bucket, timing, slack),
        "last_update": run["updated_at"],
    }


def _speed(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"median_seconds": None, "p90_seconds": None, "samples": 0}
    return {
        "median_seconds": statistics.median(values),
        "p90_seconds": _percentile(values, 0.9),
        "samples": len(values),
    }


def _throughput(store: Store, env: str, now: datetime) -> list[dict[str, Any]]:
    """Verified and merged counts per UTC day, most recent last."""
    env_runs = {str(run["id"]) for run in store.list_runs(env)}
    verified: dict[str, set[str]] = defaultdict(set)
    merged: dict[str, set[str]] = defaultdict(set)
    for event in store.all_events():
        run_id = str(event["run_id"])
        if run_id not in env_runs:
            continue
        day = str(event["at"])[:10]
        if event["kind"] == "verified":
            verified[day].add(run_id)
        elif event["kind"] == "state" and event["to_state"] == State.MERGED.value:
            merged[day].add(run_id)
    days = []
    for offset in range(THROUGHPUT_DAYS - 1, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        days.append(
            {"day": day, "verified": len(verified[day]), "merged": len(merged[day])}
        )
    return days


def _health(
    store: Store, env: str, runs: list[dict[str, Any]], now: datetime, stale: int
) -> dict[str, Any]:
    beats = {str(row["component"]): row for row in store.heartbeats()}
    worker = beats.get("worker")
    worker_at = str(worker["at"]) if worker else None
    worker_age = _seconds_between(worker_at, now.isoformat()) if worker_at else None
    undelivered = [r for r in runs if r["slack"]["status"] in {"failed", "pending"}]
    last_error = next(
        (r["slack"]["last_error"] for r in undelivered if r["slack"]["last_error"]),
        None,
    )
    last_sent = max(
        (r["slack"]["last_sent_at"] for r in runs if r["slack"]["last_sent_at"]),
        default=None,
    )
    return {
        "github": {"last_delivery_at": store.last_delivery_at()},
        "devin": {"last_poll_at": store.last_poll_at(env)},
        "worker": {
            "last_tick_at": worker_at,
            "owner": str(worker["owner"]) if worker else None,
            "stale": worker_age is None or worker_age > stale,
            "stale_after_seconds": stale,
        },
        "slack": {
            "failed": sum(r["slack"]["failed"] for r in runs),
            "pending": sum(r["slack"]["pending"] for r in runs),
            "sent": sum(r["slack"]["sent"] for r in runs),
            "last_sent_at": last_sent,
            "last_error": last_error,
        },
    }


def build_dashboard(
    store: Store,
    env: str,
    *,
    now: datetime | None = None,
    worker_stale_after_seconds: int = 300,
) -> dict[str, Any]:
    """Everything the overview page shows, for one environment."""
    current = now or utcnow()
    rows = store.list_runs(env)
    runs = [_run_view(store, row, current) for row in rows]

    workload = {bucket: 0 for bucket in WORKLOAD_BUCKETS}
    for run in runs:
        if run["bucket"] in workload:
            workload[run["bucket"]] += 1

    results = {
        "pr_opened": sum(1 for r in runs if r["pr"]["number"] is not None),
        "verified": sum(1 for r in runs if r["verified"]),
        "merged": sum(1 for r in runs if r["state"] == State.MERGED.value),
        "closed_unmerged": sum(
            1 for r in runs if r["state"] == State.CLOSED_UNMERGED.value
        ),
    }

    speed = {
        "review_ready": _speed(
            [
                r["timing"]["review_ready_seconds"]
                for r in runs
                if r["timing"]["review_ready_seconds"] is not None
            ]
        ),
        "pr_opened": _speed(
            [
                r["timing"]["pr_opened_seconds"]
                for r in runs
                if r["timing"]["pr_opened_seconds"] is not None
            ]
        ),
    }

    attention = sorted(
        (
            {
                "task_id": r["task_id"],
                "run_id": r["run_id"],
                "issue": r["issue"],
                "state": r["state"],
                **r["attention"],
            }
            for r in runs
            if r["attention"] is not None
        ),
        key=lambda item: -(item["age_seconds"] or 0),
    )

    # One row per task: the latest run carries the row, earlier attempts are
    # in the timeline.
    latest_by_task: dict[int, dict[str, Any]] = {}
    attempts: dict[int, int] = defaultdict(int)
    for run in runs:
        attempts[run["task_id"]] += 1
        if run["task_id"] not in latest_by_task:
            latest_by_task[run["task_id"]] = run
    tasks = [
        {**run, "attempts": attempts[task_id]}
        for task_id, run in latest_by_task.items()
    ]

    data_points = [r["last_update"] for r in runs] + [store.last_delivery_at()]
    data_points += [
        str(row["at"]) for row in store.heartbeats() if row["component"] != "api"
    ]
    data_as_of = max((p for p in data_points if p), default=None)

    return {
        "env": env,
        "envs_available": store.count_runs_by_env(),
        "generated_at": current.isoformat(),
        "data_as_of": data_as_of,
        "definitions": DEFINITIONS,
        "workload": workload,
        "results": results,
        "speed": speed,
        "throughput": _throughput(store, env, current),
        "attention": attention,
        "health": _health(store, env, runs, current, worker_stale_after_seconds),
        "tasks": tasks,
        "totals": {
            "tasks": len(tasks),
            "runs": len(runs),
            "acus": sum(float(r["session"]["acus_consumed"] or 0.0) for r in runs),
        },
    }


def build_timeline(
    store: Store, task_id: int, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """One task's story: every run, every event, every message, in order."""
    task = store.get_task(task_id)
    if task is None:
        return None
    current = now or utcnow()
    repo = str(task["repo"])
    issue_number = int(task["issue_number"])
    run_rows = {
        str(run["id"]): run for run in store.list_runs() if run["task_id"] == task_id
    }
    runs = [
        _run_view(store, run_rows[run_id], current)
        for run_id in sorted(run_rows, key=lambda rid: str(run_rows[rid]["created_at"]))
    ]
    events = [
        {
            "at": str(event["at"]),
            "run_id": str(event["run_id"]),
            "kind": str(event["kind"]),
            "from_state": event["from_state"],
            "to_state": event["to_state"],
            "reason": event["reason"],
            "detail": _json(event["detail"]),
        }
        for event in store.events_for_task(task_id)
    ]
    notifications = [
        {
            "at": str(row["created_at"]),
            "sent_at": row["sent_at"],
            "run_id": row["run_id"],
            "kind": str(row["kind"]),
            "reason": row["reason"],
            "destination": str(row["destination"]),
            "state": str(row["state"]),
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
        }
        for row in store.notifications_for_task(task_id)
    ]
    latest = runs[-1] if runs else None
    review = None
    if latest is not None:
        merged_by = None
        for event in reversed(events):
            if event["kind"] == "pr" and event["reason"] == "merged":
                merged_by = (event["detail"] or {}).get("merged_by") or None
                break
        if latest["state"] == State.MERGED.value:
            outcome = "merged"
        elif latest["state"] == State.CLOSED_UNMERGED.value:
            outcome = "closed unmerged"
        else:
            outcome = "pending"
        review = {
            "review_state": latest["pr"]["review_state"],
            "outcome": outcome,
            "merged_sha": latest["pr"]["merged_sha"],
            "merged_by": merged_by,
        }
    return {
        "task_id": task_id,
        "repo": repo,
        "issue": {
            "number": issue_number,
            "title": str(task["issue_title"]),
            "state": str(task["issue_state"]),
            "labels": _json(task["labels"]) or [],
            "url": f"https://github.com/{repo}/issues/{issue_number}",
        },
        "runs": runs,
        "events": events,
        "notifications": notifications,
        "review": review,
        "generated_at": current.isoformat(),
    }
