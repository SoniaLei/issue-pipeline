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

A third layer sits beside those two: what Devin reports about its own work
(ACUs, session size, its analysis of the session, organization-level PR
counts). It answers *what did this cost and what did the provider think* and
is shown under those labels; it never decides whether a run succeeded, and
where it disagrees with the store the disagreement is shown as drift.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import DEFAULT_REVIEW_GATE_MODE
from app.notifications import needs_human_text
from app.review_gate import checks_passed, gate_state, is_verified as gate_verified
from app.states import is_terminal, State
from app.store import Store, utcnow

DEFINITIONS: dict[str, str] = {
    "pr_opened": "A pull request exists on GitHub for the run. Checks may be pending.",
    "checks_passed": (
        "Every check suite GitHub reported for the PR's current head commit "
        "completed successfully. A new commit resets this to unknown."
    ),
    "verified": (
        "Checks passed on the current head and, when the review gate is "
        "required, Devin Review of that same commit reported no findings on "
        "GitHub. A new commit resets both."
    ),
    "review_gate": (
        "Devin Review of the PR's current head. Progress (pending, running, "
        "completed, errored, skipped, unavailable) is what the pr-reviews API "
        "says; the verdict (clear or N findings) is the bot's review on GitHub "
        "for that exact commit. Unknown, errored or unreviewed is never clear."
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
    "cost": (
        "ACUs are Devin's own figures. Billing-grade daily consumption is used "
        "when the provider has published it; otherwise the session's running "
        "total, marked as such. Neither changes a run's outcome."
    ),
    "session_size": (
        "Devin's size class for the session (XS-XL); L and XL are the provider's "
        "own signal that a session ran long or went back and forth."
    ),
    "drift": (
        "Devin's organization counts for the pipeline's service user, compared "
        "with what GitHub told this service. GitHub stays authoritative; a gap "
        "means a session or PR this service did not track, or one Devin "
        "counted differently."
    ),
}

SESSION_SIZES = ("xs", "s", "m", "l", "xl")
ACTION_ITEM_TYPES = (
    "machine_setup",
    "repo_config",
    "knowledge",
    "prompt_improvement",
    "external",
    "other",
)

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
MAX_THROUGHPUT_DAYS = 90


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


def is_verified(
    run: sqlite3.Row,
    review: sqlite3.Row | None = None,
    mode: str = DEFAULT_REVIEW_GATE_MODE,
) -> bool:
    """Checks passed on the current head and the review gate satisfied for
    it (D-033). ``review`` is the pr_reviews row for the run's current head."""
    return gate_verified(run, review, mode)


def bucket_for(run: sqlite3.Row, verified: bool) -> str:
    state = State(str(run["state"]))
    if state is State.PR_OPEN and verified:
        return "awaiting_review"
    return _BUCKET[state]


def _review_view(
    run: sqlite3.Row, review: sqlite3.Row | None, mode: str, now: datetime
) -> dict[str, Any]:
    """Where Devin Review stands for the run's current head.

    ``state`` is the gate word (see ``review_gate.gate_state``); ``status`` is
    the API's progress verbatim; ``findings`` is the count GitHub carries for
    this exact commit. A row for an older head is not shown here: it belongs
    to a commit that no longer exists on the PR.
    """
    head = run["head_sha"]
    if review is not None and review["head_sha"] != head:
        review = None
    state = gate_state(review, mode)
    read_at = review["status_at"] if review is not None else None
    return {
        "mode": mode,
        "state": state,
        "head_sha": head,
        "status": review["status"] if review is not None else None,
        "requested_at": review["requested_at"] if review is not None else None,
        "status_at": read_at,
        "age_seconds": _seconds_between(read_at, now.isoformat()) if read_at else None,
        "attempts": int(review["attempts"]) if review is not None else 0,
        "last_error": review["last_error"] if review is not None else None,
        "findings": review["findings"] if review is not None else None,
        "findings_by_kind": (
            _json(review["findings_by_kind"]) or {} if review is not None else {}
        ),
        "review_url": review["review_url"] if review is not None else None,
        "verdict_at": review["verdict_at"] if review is not None else None,
        "satisfied": mode != "required" or state == "clear",
    }


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


def _insights_view(store: Store, run: sqlite3.Row) -> dict[str, Any]:
    """Devin's account of the session's cost and its own review of it.

    ``cost.acus`` picks the best figure available and names its source:
    ``billing`` (daily consumption), ``session`` (the insights total), or
    ``poll`` (the last snapshot the worker saw). ``None`` means the provider has
    published nothing yet, which is not the same as zero.
    """
    row = store.insights_for_run(str(run["id"]))
    poll_acus = run["acus_consumed"]
    if row is None:
        acus = float(poll_acus) if poll_acus is not None else None
        return {
            "available": False,
            "fetched_at": None,
            "cost": {
                "acus": acus,
                "source": "poll" if acus is not None else None,
                "billed_acus": None,
                "by_day": [],
            },
            "session_size": None,
            "category": None,
            "messages": None,
            "analysis_status": None,
            "analysis": None,
            "last_error": None,
        }
    billed = row["billed_acus"]
    if billed is not None and float(billed) > 0:
        acus, source = float(billed), "billing"
    elif row["acus_consumed"] is not None:
        acus, source = float(row["acus_consumed"]), "session"
    elif poll_acus is not None:
        acus, source = float(poll_acus), "poll"
    else:
        acus, source = None, None
    analysis = _json(row["analysis"])
    summary = None
    if analysis is not None:
        skills = analysis.get("skill_usage") or {}
        prompt = analysis.get("suggested_prompt") or {}
        summary = {
            "issues": [
                {
                    "title": item.get("title"),
                    "issue": item.get("issue"),
                    "impact": item.get("impact"),
                    "label": item.get("label"),
                }
                for item in analysis.get("issues") or []
            ],
            "action_items": [
                {"type": item.get("type", "other"), "text": item.get("action_item")}
                for item in analysis.get("action_items") or []
            ],
            "skills": {
                "good": [
                    {"name": s.get("skill_name"), "reason": s.get("reason")}
                    for s in skills.get("good_usages") or []
                ],
                "bad": [
                    {"name": s.get("skill_name"), "reason": s.get("reason")}
                    for s in skills.get("bad_usages") or []
                ],
            },
            "suggested_prompt": prompt.get("suggested_prompt") or None,
            "classification": analysis.get("classification"),
        }
    return {
        "available": True,
        "fetched_at": str(row["fetched_at"]),
        "cost": {
            "acus": acus,
            "source": source,
            "billed_acus": float(billed) if billed is not None else None,
            "by_day": _json(row["consumption"]) or [],
        },
        "session_size": row["session_size"],
        "category": row["category"],
        "messages": {
            "user": row["num_user_messages"],
            "devin": row["num_devin_messages"],
        },
        "analysis_status": row["analysis_status"],
        "analysis": summary,
        "last_error": row["last_error"],
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
    run: sqlite3.Row,
    bucket: str,
    timing: dict[str, Any],
    slack: dict[str, Any],
    review: dict[str, Any],
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
    elif bucket == "pr_open" and review["state"] == "findings":
        count = int(review["findings"] or 0)
        reason = "review_findings"
        blocker = (
            f"Devin Review left {count} finding{'s' if count != 1 else ''} "
            f"on the current PR head"
        )
        action = (
            "read the review on GitHub; fix in the session (a new commit is "
            "re-reviewed) or dismiss the finding with a reason"
        )
    elif (
        bucket == "pr_open"
        and review["mode"] == "required"
        and review["state"] in {"errored", "cancelled", "skipped", "unavailable"}
    ):
        reason = f"review_{review['state']}"
        blocker = f"Devin Review of the current head {review['state']}"
        action = (
            "operator: check the pr-reviews API and the Devin GitHub app on the "
            "repo; push a new commit to request another review, or set "
            "REVIEW_GATE_MODE=advisory to verify on checks alone"
        )
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


def _run_view(
    store: Store, run: sqlite3.Row, now: datetime, mode: str
) -> dict[str, Any]:
    events = store.events_for_run(str(run["id"]))
    review_row = store.review_for_head(str(run["id"]), run["head_sha"])
    verified = is_verified(run, review_row, mode)
    review = _review_view(run, review_row, mode, now)
    bucket = bucket_for(run, verified)
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
        "verified": verified,
        "checks_passed": checks_passed(run),
        "review": review,
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
        "insights": _insights_view(store, run),
        "slack": slack,
        "timing": timing,
        "attention": _attention(run, bucket, timing, slack, review),
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


def _throughput(
    store: Store, env_runs: set[str], now: datetime, days: int
) -> list[dict[str, Any]]:
    """Verified and merged counts per UTC day, most recent last."""
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
    series = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        series.append(
            {"day": day, "verified": len(verified[day]), "merged": len(merged[day])}
        )
    return series


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
        "github": {
            "scope": "all environments",
            "last_delivery_at": store.last_delivery_at(),
        },
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


def _ratio(total: float, count: int) -> float | None:
    return total / count if count else None


def _cost(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Cost and efficiency for one environment, from the provider's figures.

    Denominators are GitHub outcomes (PRs opened, verified, merged); numerators
    are Devin's ACUs. Spend is attributed to a run only when the provider has
    published a figure for its session; runs without one are counted under
    ``coverage.no_cost_yet`` rather than silently treated as free.
    """
    with_session = [r for r in runs if r["session"]["id"]]
    priced = [r for r in with_session if r["insights"]["cost"]["acus"] is not None]
    acus = [float(r["insights"]["cost"]["acus"]) for r in priced]
    total = sum(acus)

    # Ratios are computed over priced runs only, so an unpriced PR neither
    # reads as free nor dilutes the cost of the ones Devin has priced. The
    # denominators say how many outcomes that covers, and how many it does not.
    opened = [r for r in priced if r["pr"]["number"] is not None]
    verified = [r for r in priced if r["verified"]]
    merged = [r for r in priced if r["state"] == State.MERGED.value]
    unpriced_with_pr = sum(
        1
        for r in with_session
        if r["insights"]["cost"]["acus"] is None and r["pr"]["number"] is not None
    )
    acus_without_pr = sum(
        float(r["insights"]["cost"]["acus"])
        for r in priced
        if r["pr"]["number"] is None and is_terminal(State(str(r["state"])))
    )

    sizes = {size: 0 for size in SESSION_SIZES}
    for r in with_session:
        size = r["insights"]["session_size"]
        if size in sizes:
            sizes[size] += 1
    sized = sum(sizes.values())

    categories: dict[str, int] = defaultdict(int)
    action_items: dict[str, int] = {kind: 0 for kind in ACTION_ITEM_TYPES}
    top_actions: list[dict[str, Any]] = []
    skills_good = skills_bad = 0
    analysed = 0
    user_messages: list[float] = []
    for r in with_session:
        insights = r["insights"]
        if insights["category"]:
            categories[str(insights["category"])] += 1
        if insights["messages"] and insights["messages"]["user"] is not None:
            user_messages.append(float(insights["messages"]["user"]))
        analysis = insights["analysis"]
        if analysis is None:
            continue
        analysed += 1
        for item in analysis["action_items"]:
            kind = item["type"] if item["type"] in action_items else "other"
            action_items[kind] += 1
            if len(top_actions) < 10:
                top_actions.append(
                    {
                        "type": kind,
                        "text": item["text"],
                        "issue_number": r["issue"]["number"],
                        "task_id": r["task_id"],
                    }
                )
        skills_good += len(analysis["skills"]["good"])
        skills_bad += len(analysis["skills"]["bad"])

    sources: dict[str, int] = defaultdict(int)
    for r in priced:
        sources[str(r["insights"]["cost"]["source"])] += 1

    return {
        # Sessions exist but none is priced yet: the total is unknown, not 0.
        "acus_total": total if acus or not with_session else None,
        "acus_median_per_run": statistics.median(acus) if acus else None,
        "acus_p90_per_run": _percentile(acus, 0.9) if acus else None,
        "acus_per_pr_opened": _ratio(total, len(opened)) if opened else None,
        "acus_per_verified_pr": _ratio(total, len(verified)) if verified else None,
        "acus_per_merged_pr": _ratio(total, len(merged)) if merged else None,
        "acus_without_pr": acus_without_pr,
        "denominators": {
            "pr_opened": len(opened),
            "verified": len(verified),
            "merged": len(merged),
            "unpriced_with_pr": unpriced_with_pr,
        },
        "sources": dict(sources),
        "coverage": {
            "runs_with_session": len(with_session),
            "priced": len(priced),
            "no_cost_yet": len(with_session) - len(priced),
            "billing_grade": sources.get("billing", 0),
            "analysed": analysed,
            "analysis_pending": sum(
                1
                for r in with_session
                if r["insights"]["analysis"] is None
                and r["insights"]["analysis_status"] not in {"failed"}
            ),
        },
        "session_sizes": sizes,
        "large_share": _ratio(float(sizes["l"] + sizes["xl"]), sized),
        "categories": dict(categories),
        "user_messages_median": (
            statistics.median(user_messages) if user_messages else None
        ),
        "action_items": action_items,
        "top_action_items": top_actions,
        "skills": {"good": skills_good, "bad": skills_bad},
        "fetched_at": max(
            (r["insights"]["fetched_at"] for r in runs if r["insights"]["fetched_at"]),
            default=None,
        ),
    }


def _drift(
    store: Store, env: str, runs: list[dict[str, Any]], results: dict[str, Any]
) -> dict[str, Any]:
    """Devin's organization counts against the store's, as a cross-check.

    The pipeline side counts what GitHub reported for runs in this
    environment; the provider side is Devin's count for the same service user
    over a window that covers every run. They should agree. When they do not,
    the gap is reported as drift, not reconciled away.
    """
    row = store.provider_metrics(env)
    pipeline = {
        "sessions_created": sum(1 for r in runs if r["session"]["id"]),
        "prs_opened": results["pr_opened"],
        "prs_merged": results["merged"],
    }
    if row is None:
        return {
            "status": "unavailable",
            "attempted_at": None,
            "fetched_at": None,
            "window": None,
            "service_user_ids": [],
            "provider": None,
            "pipeline": pipeline,
            "drift": None,
            "last_error": None,
        }
    metrics = _json(row["metrics"])
    provider = None
    drift = None
    status = "unavailable"
    if metrics is not None:
        provider = {
            "sessions_created": int(metrics.get("sessions_created") or 0),
            "prs_created": int(metrics.get("prs_created") or 0),
            "prs_opened": int(metrics.get("prs_opened") or 0),
            "prs_merged": int(metrics.get("prs_merged") or 0),
            "prs_closed": int(metrics.get("prs_closed") or 0),
            "sessions_with_merged_prs": int(
                metrics.get("sessions_with_merged_prs") or 0
            ),
            "avg_acus_per_session": metrics.get("avg_acus_per_session"),
            "sessions_by_size": metrics.get("sessions_by_size") or {},
        }
        drift = {
            "sessions": provider["sessions_created"] - pipeline["sessions_created"],
            "prs": provider["prs_created"] - pipeline["prs_opened"],
            "merged": provider["prs_merged"] - pipeline["prs_merged"],
        }
        status = "drift" if any(drift.values()) else "ok"
    return {
        "status": status,
        "attempted_at": str(row["attempted_at"]),
        "fetched_at": row["fetched_at"],
        "window": (
            {"after": row["window_after"], "before": row["window_before"]}
            if row["fetched_at"]
            else None
        ),
        "service_user_ids": _json(row["service_user_ids"]) or [],
        "provider": provider,
        "pipeline": pipeline,
        "drift": drift,
        "last_error": row["last_error"],
    }


def build_dashboard(
    store: Store,
    env: str,
    *,
    now: datetime | None = None,
    worker_stale_after_seconds: int = 300,
    review_gate_mode: str = DEFAULT_REVIEW_GATE_MODE,
    repo: str | None = None,
    days: int = THROUGHPUT_DAYS,
) -> dict[str, Any]:
    """Everything the overview page shows, for one environment.

    ``repo`` narrows every per-run figure to one repository. The Devin
    analytics cross-check stays across all repositories, because Devin counts
    per service user, not per repository.

    ``days`` is the length of the throughput series, in UTC days ending today.
    """
    if not 1 <= days <= MAX_THROUGHPUT_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_THROUGHPUT_DAYS}")
    current = now or utcnow()
    rows = store.list_runs(env)
    all_runs = [_run_view(store, row, current, review_gate_mode) for row in rows]
    # GitHub names are case-insensitive: one entry per repository, under the
    # first spelling seen.
    spelling: dict[str, str] = {}
    repos: dict[str, int] = defaultdict(int)
    for run in all_runs:
        key = run["repo"].lower()
        spelling.setdefault(key, run["repo"])
        repos[key] += 1
    wanted = repo.lower() if repo else None
    runs = [r for r in all_runs if wanted is None or r["repo"].lower() == wanted]
    chosen_repo = spelling.get(wanted, repo) if wanted else None

    workload = {bucket: 0 for bucket in WORKLOAD_BUCKETS}
    for run in runs:
        if run["bucket"] in workload:
            workload[run["bucket"]] += 1

    results = _results(runs)

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

    health = _health(store, env, runs, current, worker_stale_after_seconds)
    health["devin_analytics"] = {
        **_drift(store, env, all_runs, _results(all_runs)),
        "scope": "all repositories",
    }
    last_call = store.last_review_call(env)

    return {
        "env": env,
        "envs_available": store.count_runs_by_env(),
        "repo": chosen_repo,
        "repos_available": [
            {"repo": spelling[key], "runs": repos[key]} for key in sorted(repos)
        ],
        "generated_at": current.isoformat(),
        "data_as_of": data_as_of,
        "definitions": DEFINITIONS,
        "review_gate": {
            "mode": review_gate_mode,
            "states": _review_states(runs),
            "last_call_at": (
                str(last_call["status_at"])
                if last_call is not None and last_call["status_at"]
                else None
            ),
        },
        "workload": workload,
        "results": results,
        "speed": speed,
        "throughput_days": days,
        "throughput": _throughput(store, {r["run_id"] for r in runs}, current, days),
        "cost": _cost(runs),
        "attention": attention,
        "health": health,
        "tasks": tasks,
        "totals": {
            "tasks": len(tasks),
            "runs": len(runs),
            "acus": sum(float(r["session"]["acus_consumed"] or 0.0) for r in runs),
        },
    }


def _results(runs: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "pr_opened": sum(1 for r in runs if r["pr"]["number"] is not None),
        "checks_passed": sum(1 for r in runs if r["checks_passed"]),
        "verified": sum(1 for r in runs if r["verified"]),
        "merged": sum(1 for r in runs if r["state"] == State.MERGED.value),
        "closed_unmerged": sum(
            1 for r in runs if r["state"] == State.CLOSED_UNMERGED.value
        ),
    }


def _review_states(runs: list[dict[str, Any]]) -> dict[str, int]:
    """How many open PRs sit in each gate state (terminal runs excluded)."""
    counts: dict[str, int] = defaultdict(int)
    for run in runs:
        if run["pr"]["number"] is None or is_terminal(State(str(run["state"]))):
            continue
        counts[str(run["review"]["state"])] += 1
    return dict(counts)


def build_timeline(
    store: Store,
    task_id: int,
    *,
    now: datetime | None = None,
    review_gate_mode: str = DEFAULT_REVIEW_GATE_MODE,
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
        _run_view(store, run_rows[run_id], current, review_gate_mode)
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
            "threaded": row["thread_ts"] is not None,
            "reaction": row["reaction"],
            "reaction_error": row["reaction_error"],
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
            "devin_review": latest["review"],
            "devin_review_history": [
                {
                    "head_sha": str(row["head_sha"]),
                    "current": row["head_sha"] == latest["pr"]["head_sha"],
                    "status": row["status"],
                    "findings": row["findings"],
                    "findings_by_kind": _json(row["findings_by_kind"]) or {},
                    "review_url": row["review_url"],
                    "requested_at": row["requested_at"],
                    "status_at": row["status_at"],
                    "verdict_at": row["verdict_at"],
                    "last_error": row["last_error"],
                }
                for row in store.reviews_for_run(latest["run_id"])
            ],
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
