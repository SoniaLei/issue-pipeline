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
# ruff: noqa: E501
"""Read-only operational views. Historical milestones require recorded events."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from statistics import median
from typing import Any
from urllib.parse import urlparse

from app.states import is_terminal, State
from app.store import Store


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_overview(
    store: Store,
    *,
    env: str = "sim",
    repo: str | None = None,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).date()
    daily = {
        str(start + timedelta(days=i)): {"pr_opened": 0, "merged": 0}
        for i in range(days)
    }
    runs: list[dict[str, Any]] = []
    notes: Counter[str] = Counter()
    durations: list[float] = []
    milestones_seen: set[tuple[str, int, str]] = set()
    for task in store.list_tasks():
        if repo and str(task["repo"]).lower() != repo.lower():
            continue
        for row in store.runs_for_task(int(task["id"])):
            if row["env"] != env:
                continue
            events = [dict(e) for e in store.events_for_run(str(row["id"]))]
            notifications = [
                dict(n)
                for n in store.notifications_for_task(int(task["id"]))
                if n["run_id"] == row["id"]
            ]
            notes.update(str(n["state"]) for n in notifications)
            first: dict[str, str] = {}
            for event in events:
                if event["kind"] != "snapshot":
                    first.setdefault(event["to_state"], event["recorded_at"])
            record_throughput(first, row, task, daily, milestones_seen)
            if "pr_open" in first and row["approved_at"]:
                duration = (
                    instant(first["pr_open"]) - instant(row["approved_at"])
                ).total_seconds()
                if duration >= 0:
                    durations.append(duration)
            end = (
                instant(events[-1]["recorded_at"])
                if is_terminal(State(row["state"]))
                else now
            )
            runs.append(
                {
                    "run_id": row["id"],
                    "repo": task["repo"],
                    "issue_number": task["issue_number"],
                    "title": task["issue_title"],
                    "state": row["state"],
                    "env": env,
                    "age_seconds": (
                        None
                        if is_terminal(State(row["state"]))
                        and any(e["kind"] == "snapshot" for e in events)
                        else max(0, (end - instant(row["created_at"])).total_seconds())
                    ),
                    "created_at": row["created_at"],
                    "last_state_change_at": events[-1]["recorded_at"]
                    if events
                    else None,
                    "last_provider_poll_at": row["session_polled_at"],
                    "pr_url": row["pr_url"],
                    "session_url": row["session_url"],
                    "head_sha": row["head_sha"],
                    "merged_sha": row["merged_sha"],
                    "checks": "not evaluated",
                    "acus_consumed": row["acus_consumed"],
                    "failure_reason": row["failure_reason"],
                    "events": events,
                    "notifications": [
                        {
                            k: n[k]
                            for k in (
                                "kind",
                                "state",
                                "attempts",
                                "created_at",
                                "sent_at",
                            )
                        }
                        for n in notifications
                    ],
                    "history_complete": not any(
                        e["kind"] == "snapshot" for e in events
                    ),
                }
            )
    states = Counter(r["state"] for r in runs)
    known = [r["acus_consumed"] for r in runs if r["acus_consumed"] is not None]
    return {
        "generated_at": now.isoformat(),
        "env": env,
        "repo": repo,
        "period": {
            "days": days,
            "start": str(start),
            "end": str(now.date()),
            "timezone": "UTC",
        },
        "runs_by_state": dict(states),
        "run_count": len(runs),
        "issue_count": len({(r["repo"], r["issue_number"]) for r in runs}),
        "active": sum(states[s] for s in ("starting", "running")),
        "failed": sum(states[s] for s in ("failed", "expired", "no_output")),
        "notifications_by_state": dict(notes),
        "known_acus": sum(known) if known else None,
        "usage_unknown_runs": len(runs) - len(known),
        "median_seconds_to_pr": median(durations) if durations else None,
        "time_to_pr_sample_count": len(durations),
        "daily_throughput": [{"date": day, **counts} for day, counts in daily.items()],
        "verification": "Not evaluated: CI reconciliation is outside the current worker.",
        "limitations": "Throughput uses observed transitions, not GitHub event time. Legacy snapshots are excluded. Current counts and time-to-PR cover all matching runs; only throughput uses the selected period. Provider poll timestamps do not prove worker health.",
        "runs": runs,
    }


def record_throughput(
    first: dict[str, str],
    row: Any,
    task: Any,
    daily: dict[str, dict[str, int]],
    seen: set[tuple[str, int, str]],
) -> None:
    for state, name in (("pr_open", "pr_opened"), ("merged", "merged")):
        if state not in first or row["pr_number"] is None:
            continue
        key = (str(task["repo"]), int(row["pr_number"]), name)
        date = str(instant(first[state]).date())
        if key not in seen and date in daily:
            daily[date][name] += 1
            seen.add(key)


def duration_label(seconds: float | None) -> str:
    return (
        f"{seconds / 60:.1f} min" if seconds is not None else "Unknown (legacy history)"
    )


def safe(value: Any) -> str:
    return escape(str(value if value is not None else "Not available"))


def link(url: Any, label: str) -> str:
    if url and urlparse(str(url)).scheme == "https":
        return f'<a href="{safe(url)}" rel="noreferrer">{safe(label)}</a>'
    return "Not available"


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe(title)}</title><style>
body{{font:16px system-ui;background:#f3f6f8;color:#192e40;margin:0}}
main{{max-width:1200px;margin:auto;padding:36px}}h1{{font-size:34px}}
a{{color:#056b91}}.muted{{color:#526473}}.cards{{display:flex;flex-wrap:wrap;gap:16px}}
.card{{background:white;padding:20px;min-width:130px;border-top:4px solid #087f8c}}
strong{{font-size:28px;display:block}}table{{width:100%;border-collapse:collapse;background:white}}
th,td{{padding:12px;text-align:left;border-bottom:1px solid #d9e2e7;overflow-wrap:anywhere}}
.scroll{{overflow:auto}}label{{margin-right:16px}}select,input,button{{font:inherit;padding:7px}}
.notice{{background:#fff4d5;padding:16px}}code{{overflow-wrap:anywhere}}
</style><main>{body}</main></html>"""


def render_dashboard(report: dict[str, Any]) -> str:
    states = report["runs_by_state"]
    options = "".join(
        f"<option {'selected' if report['env'] == v else ''}>{v}</option>"
        for v in ("sim", "live")
    )
    body = f"""<h1>Engineering pipeline</h1><p class="muted">Issue delivery and operational status · {safe(report["env"]).upper()} MODE</p>
<form><label>Mode <select name="env">{options}</select></label>
<label>Repository <input name="repo" value="{safe(report["repo"] or "")}" placeholder="owner/repository"></label>
<label>Days <input type="number" name="days" min="1" max="90" value="{report["period"]["days"]}"></label><button>Apply</button></form>
<p>Report generated: {safe(report["generated_at"])}. Times in UTC.</p><div class="cards">"""
    for label, value in (
        ("Queued", states.get("queued", 0)),
        ("Running", report["active"]),
        ("PR open", states.get("pr_open", 0)),
        ("Blocked", states.get("session_blocked", 0)),
        ("Failed / expired / no output", report["failed"]),
        ("Merged", states.get("merged", 0)),
    ):
        body += f'<div class="card">{label}<strong>{value}</strong></div>'
    body += f'<p class="notice">{safe(report["verification"])} A PR opening is not a verified fix.</p>'
    seconds = report["median_seconds_to_pr"]
    duration = f"{seconds / 60:.1f} minutes" if seconds is not None else "Not available"
    body += f"<p>Median approval to PR: <b>{duration}</b> (n={report['time_to_pr_sample_count']}, all recorded runs). Known ACUs: {safe(report['known_acus'])}; usage unknown for {report['usage_unknown_runs']} runs.</p>"
    body += "<h2>Observed throughput</h2><table><tr><th>Date (UTC)</th><th>PRs opened</th><th>Merged</th></tr>"
    for day in report["daily_throughput"]:
        body += f"<tr><td>{day['date']}</td><td>{day['pr_opened']}</td><td>{day['merged']}</td></tr>"
    body += '</table><h2>Tasks and attempts</h2><div class="scroll"><table><tr><th>Issue</th><th>State</th><th>Age / duration</th><th>Last provider poll</th><th>PR</th><th>Details</th></tr>'
    for run in report["runs"]:
        body += f'<tr><td>{safe(run["repo"])} #{run["issue_number"]}<br>{safe(run["title"])}</td><td>{safe(run["state"])}</td><td>{duration_label(run["age_seconds"])}</td><td>{safe(run["last_provider_poll_at"])}</td><td>{link(run["pr_url"], "Open PR")}</td><td><a href="/runs/{safe(run["run_id"])}">Timeline</a></td></tr>'
    if not report["runs"]:
        body += '<tr><td colspan="6">No runs match this mode and repository.</td></tr>'
    body += "</table></div><h2>All run states</h2>"
    body += "<p>" + safe(report["runs_by_state"]) + "</p><h2>Needs attention</h2>"
    attention = [
        r
        for r in report["runs"]
        if r["state"] in ("session_blocked", "failed", "expired", "no_output")
    ]
    body += (
        "".join(
            f"<p>{safe(r['run_id'])}: {safe(r['failure_reason'] or r['state'])}</p>"
            for r in attention
        )
        or "<p>No blocked or unsuccessful runs in this view.</p>"
    )
    body += f'<h2>Notification delivery</h2><p>{safe(report["notifications_by_state"])}</p><p class="muted">{safe(report["limitations"])}</p>'
    return page("Pipeline observability", body)


def render_run(run: dict[str, Any]) -> str:
    body = f'<a href="/dashboard?env={safe(run["env"])}">Back to dashboard</a><h1>Run {safe(run["run_id"])}</h1><p>{safe(run["title"])} · {safe(run["state"])} · {safe(run["env"])}</p>'
    body += f"<p>{link(run['pr_url'], 'Pull request')} · {link(run['session_url'], 'Devin session')}</p><p>Head SHA: {safe(run['head_sha'])}<br>Merge SHA: {safe(run['merged_sha'])}<br>Checks: not evaluated</p>"
    if not run["history_complete"]:
        body += '<p class="notice">Legacy run: history begins with a snapshot. Earlier milestone times are unknown.</p>'
    body += "<h2>State timeline</h2><table><tr><th>Recorded at</th><th>Kind</th><th>From</th><th>To</th></tr>"
    for e in run["events"]:
        body += f"<tr><td>{safe(e['recorded_at'])}</td><td>{safe(e['kind'])}</td><td>{safe(e['from_state'])}</td><td>{safe(e['to_state'])}</td></tr>"
    body += "</table><h2>Notifications</h2>"
    for n in run["notifications"]:
        body += f"<p>{safe(n['kind'])}: {safe(n['state'])} · attempts {n['attempts']} · sent {safe(n['sent_at'])}</p>"
    body += f"<p>Failure reason: {safe(run['failure_reason'])}</p>"
    return page("Run details", body)
