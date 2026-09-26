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
"""The task report.

Deliberately small: what state each task is in, what it cost, and whether the
notifications about it actually arrived. Notification state is reported
separately from task state because they fail independently, and a report that
conflates them hides the case where the work succeeded and nobody heard.
"""

from __future__ import annotations

from typing import Any

from app.states import is_terminal, State
from app.store import Store


def build_report(store: Store) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    totals = {"tasks": 0, "runs": 0, "acus": 0.0}
    by_state: dict[str, int] = {}
    notification_states: dict[str, int] = {}

    for task in store.list_tasks():
        task_id = int(task["id"])
        runs = store.runs_for_task(task_id)
        totals["tasks"] += 1
        run_views: list[dict[str, Any]] = []
        for run in runs:
            totals["runs"] += 1
            acus = float(run["acus_consumed"] or 0.0)
            totals["acus"] += acus
            state = str(run["state"])
            by_state[state] = by_state.get(state, 0) + 1
            run_views.append(
                {
                    "run_id": str(run["id"]),
                    "state": state,
                    "terminal": is_terminal(State(state)),
                    "env": str(run["env"]),
                    "approved_by": run["approved_by"],
                    "approval_revoked_by": run["approval_revoked_by"],
                    "session_url": run["session_url"],
                    "provider_status": run["session_status"],
                    "provider_status_detail": run["session_status_detail"],
                    "acus_consumed": acus,
                    "max_acu_limit": run["max_acu_limit"],
                    "branch": run["branch"],
                    "pr_url": run["pr_url"],
                    "pr_number": run["pr_number"],
                    "merged_sha": run["merged_sha"],
                    "failure_reason": run["failure_reason"],
                }
            )

        notifications = [
            {
                "kind": str(row["kind"]),
                "reason": row["reason"],
                "destination": str(row["destination"]),
                "state": str(row["state"]),
                "attempts": int(row["attempts"]),
                "last_error": row["last_error"],
            }
            for row in store.notifications_for_task(task_id)
        ]
        for row in notifications:
            key = str(row["state"])
            notification_states[key] = notification_states.get(key, 0) + 1

        tasks.append(
            {
                "task_id": task_id,
                "repo": str(task["repo"]),
                "issue_number": int(task["issue_number"]),
                "issue_title": str(task["issue_title"]),
                "issue_state": str(task["issue_state"]),
                "runs": run_views,
                "notifications": notifications,
            }
        )

    return {
        "totals": totals,
        "runs_by_state": by_state,
        "notifications_by_state": notification_states,
        "tasks": tasks,
    }


def render_text(report: dict[str, Any]) -> str:
    """A plain-text rendering, for a terminal or a log."""
    lines = [
        "issue-pipeline report",
        "=====================",
        f"tasks={report['totals']['tasks']} runs={report['totals']['runs']} "
        f"acus={report['totals']['acus']:.1f}",
        f"runs by state: {report['runs_by_state'] or '{}'}",
        f"notifications: {report['notifications_by_state'] or '{}'}",
        "",
    ]
    for task in report["tasks"]:
        lines.append(f"{task['repo']}#{task['issue_number']} — {task['issue_title']}")
        for run in task["runs"]:
            pr = run["pr_url"] or "no PR"
            reason = f" ({run['failure_reason']})" if run["failure_reason"] else ""
            lines.append(
                f"  run {run['run_id']} [{run['env']}] {run['state']}{reason}"
                f" acus={run['acus_consumed']:.1f} {pr}"
            )
        for note in task["notifications"]:
            lines.append(
                f"  note {note['kind']} -> {note['destination']} {note['state']}"
                f" attempts={note['attempts']}"
            )
        lines.append("")
    return "\n".join(lines)
