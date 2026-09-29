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
"""Ninety days of invented history for the simulated dashboard.

The scripted simulation drives one issue through the real pipeline, which
proves the wiring but leaves every chart flat. This seeds the store directly
with a longer, deterministic story so the throughput, cost, speed and
attention panels have something to show:

* volume rises over the window, dips at weekends, collapses during a holiday
  lull and spikes in a release crunch;
* a broken-baseline incident sends checks red and sessions nowhere for a week;
* after the "skills landed" day sessions get cheaper and more PRs merge;
* the last few days hold runs that are still in flight or need a human.

Rows are written with explicit timestamps and ``env='sim'`` only, so nothing
here can leak into a live report. None of it passes through intake or the
worker: it is scenery, not evidence that the pipeline works.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from app.prompts import branch_name
from app.states import State
from app.store import Store, utcnow

REPOS: tuple[tuple[str, float], ...] = (
    ("SoniaLei/superset-cognition-demo", 0.75),
    ("SoniaLei/issue-pipeline", 0.25),
)

TITLES = (
    "Dashboard filter resets when a chart is refreshed",
    "Explore: dataset search drops the schema prefix",
    "SQL Lab tab title does not update after rename",
    "Time-range picker ignores the saved default",
    "Legend overlaps the axis on narrow charts",
    "CSV export loses the column order",
    "Native filter scoping forgets excluded charts",
    "Alert email renders the wrong timezone",
    "Chart cache key ignores the row limit",
    "Pivot table totals double-count nulls",
    "Tooltip shows raw epoch for temporal columns",
    "Import of a dashboard bundle rejects valid YAML",
    "Docs: architecture schema out of date",
    "Report endpoint omits cancelled runs",
    "Slack thread reply loses the reaction",
    "Retry policy for the outbox is undocumented",
)

ACTION_ITEMS = (
    ("repo_config", "Add a scoped pytest target for superset/explore to AGENTS.md"),
    ("knowledge", "Frontend tests need NODE_OPTIONS=--max-old-space-size=8192"),
    ("machine_setup", "Pre-install Python 3.11 in the blueprint"),
    ("prompt_improvement", "Name the failing test in the issue body"),
    ("knowledge", "Explain which fixtures the dashboard tests rely on"),
    ("external", "Flaky upstream check: docker pull rate limit"),
)

LULL = range(40, 47)  # days ago: holiday, almost nothing approved
INCIDENT = range(55, 63)  # days ago: baseline broken, checks red
CRUNCH = range(19, 27)  # days ago: release crunch, lots approved
SKILLS_LANDED = 35  # days ago: sessions get cheaper and more reliable after this

NORMAL = {
    State.MERGED: 0.58,
    State.CLOSED_UNMERGED: 0.10,
    State.NO_OUTPUT: 0.10,
    State.FAILED: 0.09,
    State.EXPIRED: 0.05,
    State.CANCELLED: 0.08,
}
EARLY = {**NORMAL, State.MERGED: 0.45, State.NO_OUTPUT: 0.17, State.FAILED: 0.13}
BROKEN = {
    State.MERGED: 0.18,
    State.CLOSED_UNMERGED: 0.22,
    State.NO_OUTPUT: 0.22,
    State.FAILED: 0.23,
    State.EXPIRED: 0.10,
    State.CANCELLED: 0.05,
}
IN_FLIGHT = {
    "awaiting_approval": 0.12,
    "queued": 0.10,
    "running": 0.18,
    "session_blocked": 0.10,
    "pr_open_checks_failed": 0.14,
    "pr_open_findings": 0.14,
    "pr_open_verified": 0.22,
}


@dataclass
class _Counters:
    issue: dict[str, int]
    pr: dict[str, int]
    session: int = 0
    note: int = 0


def seed_history(
    store: Store, *, days: int = 90, seed: int = 7, now: datetime | None = None
) -> int:
    """Write ``days`` of simulated runs ending at ``now``; return how many."""
    if days <= 0:
        return 0
    rng = random.Random(seed)
    current = now or utcnow()
    counters = _Counters(
        issue={repo: 100 + 20 * i for i, (repo, _) in enumerate(REPOS)},
        pr={repo: 400 + 50 * i for i, (repo, _) in enumerate(REPOS)},
    )
    written = 0
    with store.transaction() as conn:
        for age in range(days - 1, -1, -1):
            for _ in range(_poisson(rng, _volume(age, days, current))):
                start = current - timedelta(days=age, hours=rng.uniform(0.5, 20))
                start = min(start, current - timedelta(minutes=rng.uniform(20, 180)))
                repo = _pick(rng, dict(REPOS))
                task_id = _task(conn, rng, counters, repo, start)
                outcome = _outcome(rng, age)
                _run(conn, rng, counters, task_id, repo, start, outcome, age, current)
                written += 1
                if (
                    outcome in {State.FAILED.value, State.NO_OUTPUT.value}
                    and age > 3
                    and rng.random() < 0.45
                ):
                    retry = start + timedelta(hours=rng.uniform(18, 60))
                    retry_age = (current - retry).days
                    _run(
                        conn,
                        rng,
                        counters,
                        task_id,
                        repo,
                        retry,
                        State.MERGED.value,
                        retry_age,
                        current,
                    )
                    written += 1
    return written


def _volume(age: int, days: int, now: datetime) -> float:
    trend = 1.5 + 3.5 * (1 - age / max(days, 1))
    weekday = (now - timedelta(days=age)).weekday()
    rate = trend * (0.3 if weekday >= 5 else 1.0)
    if age in LULL:
        rate *= 0.15
    if age in CRUNCH:
        rate *= 1.9
    return rate


def _outcome(rng: random.Random, age: int) -> str:
    if age <= 2:
        return _pick(rng, IN_FLIGHT)
    if age in INCIDENT:
        table = BROKEN
    elif age > SKILLS_LANDED:
        table = EARLY
    else:
        table = NORMAL
    return _pick(rng, {state.value: p for state, p in table.items()})


def _pick(rng: random.Random, weights: dict[str, float]) -> str:
    choices = list(weights)
    return rng.choices(choices, weights=[weights[c] for c in choices])[0]


def _poisson(rng: random.Random, rate: float) -> int:
    limit, k, p = math.exp(-rate), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _task(
    conn: sqlite3.Connection,
    rng: random.Random,
    counters: _Counters,
    repo: str,
    at: datetime,
) -> int:
    counters.issue[repo] += 1
    number = counters.issue[repo]
    cursor = conn.execute(
        "INSERT INTO tasks (repo, issue_number, issue_title, issue_state, labels,"
        " created_at, updated_at) VALUES (?, ?, ?, 'open', ?, ?, ?)",
        (
            repo,
            number,
            rng.choice(TITLES),
            json.dumps(["devin-ready"]),
            at.isoformat(),
            at.isoformat(),
        ),
    )
    return int(cursor.lastrowid or 0)


Event = tuple[str, str, str | None, str | None, str | None, Any]


class _Clock:
    """Moves forward through a run, never past the present."""

    def __init__(self, start: datetime, now: datetime) -> None:
        self.t = start
        self.now = now

    def advance(self, minutes: float) -> None:
        ceiling = self.now - timedelta(seconds=30)
        self.t = min(self.t + timedelta(minutes=minutes), max(self.t, ceiling))

    def at(self) -> str:
        return self.t.isoformat()


class _Run:
    """One invented run, assembled in memory and written in one go."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        rng: random.Random,
        task_id: int,
        start: datetime,
        now: datetime,
    ) -> None:
        self.conn = conn
        self.rng = rng
        self.task_id = task_id
        self.now = now
        self.id = f"h{rng.getrandbits(44):011x}"
        self.clock = _Clock(start, now)
        self.events: list[Event] = []
        self.states: list[str] = []
        self.notes: list[str] = []
        self.later: list[Callable[[], None]] = []
        self.fields: dict[str, Any] = {
            "approved_by": rng.choice(["sonialei", "sonialei", "lei-bot-reviewer"]),
            "approved_at": self.clock.at(),
            "max_acu_limit": 20,
        }

    def state(self, to: str, reason: str | None = None, minutes: float = 0.0) -> None:
        previous = self.states[-1] if self.states else None
        self.clock.advance(minutes)
        self.events.append((self.clock.at(), "state", previous, to, reason, None))
        self.states.append(to)

    def event(self, kind: str, reason: str | None, detail: Any, minutes: float) -> None:
        self.clock.advance(minutes)
        self.events.append((self.clock.at(), kind, None, None, reason, detail))

    def write(self, state: str) -> None:
        at = self.clock.at()
        self.fields.update(
            id=self.id,
            task_id=self.task_id,
            state=state,
            env="sim",
            created_at=self.events[0][0],
            updated_at=at,
            session_polled_at=at if self.fields.get("session_id") else None,
        )
        columns = list(self.fields)
        self.conn.execute(
            f"INSERT INTO runs ({', '.join(columns)})"
            f" VALUES ({', '.join('?' for _ in columns)})",
            [self.fields[c] for c in columns],
        )
        for step in self.later:
            step()
        self.conn.executemany(
            "INSERT INTO run_events (run_id, task_id, at, kind, from_state, to_state,"
            " reason, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    self.id,
                    self.task_id,
                    when,
                    kind,
                    src,
                    dst,
                    reason,
                    json.dumps(detail) if detail is not None else None,
                )
                for when, kind, src, dst, reason, detail in self.events
            ],
        )
        sent = min(self.clock.t + timedelta(seconds=2), self.now).isoformat()
        for kind in self.notes:
            self.conn.execute(
                "INSERT INTO outbox (task_id, run_id, kind, reason, fingerprint,"
                " destination, payload, state, attempts, created_at, sent_at)"
                " VALUES (?, ?, ?, ?, ?, 'engineering-updates', ?, 'sent', 1, ?, ?)",
                (
                    self.task_id,
                    self.id,
                    kind,
                    self.fields.get("failure_reason")
                    if kind == "needs_human"
                    else None,
                    f"history:{self.id}:{kind}",
                    json.dumps({"text": f"[sim history] {kind.replace('_', ' ')}"}),
                    at,
                    sent,
                ),
            )
        self.conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ? AND updated_at < ?",
            (at, self.task_id, at),
        )


def _run(
    conn: sqlite3.Connection,
    rng: random.Random,
    counters: _Counters,
    task_id: int,
    repo: str,
    start: datetime,
    outcome: str,
    age: int,
    now: datetime,
) -> None:
    run = _Run(conn, rng, task_id, start, now)
    if outcome == State.AWAITING_APPROVAL.value:
        run.state(outcome)
        run.fields["approved_by"] = run.fields["approved_at"] = None
        run.write(outcome)
        return
    run.state(State.QUEUED.value)
    if outcome == State.QUEUED.value:
        run.fields["failure_reason"] = rng.choice(["concurrency", "daily_cap"])
        run.write(outcome)
        return
    if outcome == State.CANCELLED.value:
        run.state(outcome, "approval_revoked", rng.uniform(5, 90))
        run.fields.update(
            approval_revoked_by="sonialei", approval_revoked_at=run.clock.at()
        )
        run.write(outcome)
        return
    acus = _start_session(run, counters, age)
    if outcome in {State.RUNNING.value, State.SESSION_BLOCKED.value}:
        _still_in_session(run, outcome, acus)
    elif outcome in {State.FAILED.value, State.NO_OUTPUT.value, State.EXPIRED.value}:
        _ended_without_pr(run, outcome, acus)
    else:
        _opened_pr(run, counters, repo, outcome, age, acus)


def _start_session(run: _Run, counters: _Counters, age: int) -> float:
    issue = int(
        run.conn.execute(
            "SELECT issue_number FROM tasks WHERE id = ?", (run.task_id,)
        ).fetchone()[0]
    )
    counters.session += 1
    session = f"devin-hist-{counters.session:04d}"
    run.fields.update(
        session_id=session,
        session_url=f"https://app.devin.ai/sessions/{session}",
        branch=branch_name(issue, run.id),
    )
    run.state(State.STARTING.value, minutes=run.rng.uniform(0.5, 4))
    run.event("session", "created", {"session_id": session}, 0.5)
    run.state(State.RUNNING.value, minutes=run.rng.uniform(1, 3))
    cheap = 0.7 if age <= SKILLS_LANDED else 1.0
    pricey = 1.35 if age in INCIDENT else 1.0
    return round(run.rng.lognormvariate(math.log(5.5), 0.45) * cheap * pricey, 1)


def _still_in_session(run: _Run, outcome: str, acus: float) -> None:
    run.clock.advance(run.rng.uniform(10, 120))
    run.fields.update(session_status="running", acus_consumed=round(acus * 0.5, 1))
    if outcome == State.SESSION_BLOCKED.value:
        run.state(outcome, "waiting_for_user")
        run.fields.update(failure_reason="waiting_for_user", session_status="blocked")
        run.notes.append("needs_human")
    run.write(outcome)


def _ended_without_pr(run: _Run, outcome: str, acus: float) -> None:
    minutes = run.rng.uniform(30, 200)
    if outcome == State.FAILED.value:
        reason = run.rng.choice(["acu_limit", "acu_limit", "session_error"])
        if reason == "acu_limit":
            acus = 20.0
    elif outcome == State.EXPIRED.value:
        reason, minutes, acus = "expired", 360.0, round(acus * 2.2, 1)
    else:
        reason = "no_output"
    run.state(outcome, reason, minutes)
    run.fields.update(
        failure_reason=reason,
        acus_consumed=acus,
        session_status="finished" if outcome == "no_output" else "expired",
        session_finished_at=run.clock.at(),
    )
    run.notes.append("needs_human")
    _insights(run, acus, outcome, None)
    run.write(outcome)


def _opened_pr(
    run: _Run, counters: _Counters, repo: str, outcome: str, age: int, acus: float
) -> None:
    counters.pr[repo] += 1
    number = counters.pr[repo]
    head = f"{run.rng.getrandbits(160):040x}"
    url = f"https://github.com/{repo}/pull/{number}"
    tests_added = run.rng.random() < (0.85 if age <= SKILLS_LANDED else 0.65)
    run.fields.update(
        pr_number=number,
        pr_url=url,
        pr_state="open",
        pr_draft=0,
        head_sha=head,
        checks_head_sha=head,
        acus_consumed=acus,
        session_status="finished",
        structured_output=json.dumps(
            {
                "outcome": "pr_opened",
                "tests_added": tests_added,
                "tests_run": ["pytest"],
            }
        ),
    )
    run.state(State.PR_OPEN.value, minutes=run.rng.uniform(20, 180))
    run.event("pr", "opened", {"number": number, "url": url}, 0.2)
    run.fields["session_finished_at"] = run.clock.at()
    run.notes.append("pr_opened")
    _checks_and_review(run, repo, outcome, age, head, url)
    _insights(run, acus, outcome, number)
    if outcome == State.MERGED.value:
        run.fields["review_state"] = "approved"
        run.event("review", "approved", {"by": "sonialei"}, run.rng.uniform(40, 1400))
        run.notes.append("human_review")
        run.state(outcome, minutes=run.rng.uniform(5, 600))
        run.event("pr", "merged", {"merged_by": "sonialei"}, 0)
        run.fields.update(
            pr_state="closed", merged_sha=f"{run.rng.getrandbits(160):040x}"
        )
        run.notes.append("pr_merged")
    elif outcome == State.CLOSED_UNMERGED.value:
        run.state(outcome, minutes=run.rng.uniform(60, 2400))
        run.fields["pr_state"] = "closed"
        run.notes.append("pr_closed")
    run.write(State.PR_OPEN.value if outcome.startswith("pr_open") else outcome)


def _checks_and_review(
    run: _Run, repo: str, outcome: str, age: int, head: str, url: str
) -> None:
    red = (
        outcome == "pr_open_checks_failed"
        or (outcome == State.CLOSED_UNMERGED.value and run.rng.random() < 0.5)
        or (age in INCIDENT and outcome != State.MERGED.value)
    )
    run.fields["checks_state"] = "failed" if red else "passed"
    run.event("checks", run.fields["checks_state"], {"head_sha": head}, 12.0)
    at = run.clock.at()
    run.later.append(lambda: _checks(run.conn, run.id, repo, head, red, at))
    if red:
        run.notes.append("checks_failed")
        return
    findings = run.rng.choice([1, 2, 3]) if outcome == "pr_open_findings" else 0
    run.event("review_gate", "findings" if findings else "clear", None, 8.0)
    if findings:
        run.notes.append("review_findings")
    else:
        run.event("verified", None, {"head_sha": head}, 0.1)
        run.notes.append("verified")
    at = run.clock.at()
    run.later.append(lambda: _review(run.conn, run.id, head, url, findings, at))


def _checks(
    conn: sqlite3.Connection,
    run_id: str,
    repo: str,
    head: str,
    red: bool,
    at: str,
) -> None:
    conn.execute(
        "INSERT INTO checks (run_id, head_sha, suite_id, app, status, conclusion,"
        " url, updated_at) VALUES (?, ?, ?, 'github-actions', 'completed', ?, ?, ?)",
        (
            run_id,
            head,
            f"suite-{run_id}",
            "failure" if red else "success",
            f"https://github.com/{repo}/actions",
            at,
        ),
    )


def _review(
    conn: sqlite3.Connection,
    run_id: str,
    head: str,
    url: str,
    findings: int,
    at: str,
) -> None:
    conn.execute(
        "INSERT INTO pr_reviews (run_id, head_sha, env, pr_url, status,"
        " requested_at, status_at, findings, findings_by_kind, verdict_at)"
        " VALUES (?, ?, 'sim', ?, 'completed', ?, ?, ?, ?, ?)",
        (
            run_id,
            head,
            url,
            at,
            at,
            findings,
            json.dumps({"bug": findings} if findings else {}),
            at,
        ),
    )


def _insights(run: _Run, acus: float, outcome: str, pr_number: int | None) -> None:
    rng = run.rng
    conn, run_id, session, at = (
        run.conn,
        run.id,
        run.fields["session_id"],
        run.clock.at(),
    )
    if outcome.startswith("pr_open") and rng.random() < 0.5:
        return  # the provider has not priced every fresh session yet
    size = next(
        name
        for name, ceiling in (("xs", 2), ("s", 5), ("m", 9), ("l", 15), ("xl", 1e9))
        if acus < ceiling
    )
    items = rng.sample(ACTION_ITEMS, k=rng.choice([0, 1, 1, 2]))
    analysis = {
        "issues": (
            [{"title": "Baseline tests failing before any change", "impact": "high"}]
            if outcome in {State.NO_OUTPUT.value, State.FAILED.value}
            else []
        ),
        "action_items": [{"type": t, "action_item": text} for t, text in items],
        "skill_usage": {
            "good_usages": (
                [{"skill_name": "run-baseline", "reason": "ran scoped tests first"}]
                if pr_number is not None
                else []
            ),
            "bad_usages": [],
        },
    }
    row = (
        acus,
        acus,
        json.dumps([[at[:10] + "T08:00:00+00:00", acus]]),
        size,
        rng.choice(["bug_fixing", "bug_fixing", "feature", "docs"]),
        rng.choice([0, 0, 1, 2]),
        rng.randint(8, 40),
        json.dumps(analysis),
    )
    run.later.append(
        lambda: _insert_insights(conn, run_id, str(session), at, outcome, row)
    )


def _insert_insights(
    conn: sqlite3.Connection,
    run_id: str,
    session: str,
    at: str,
    outcome: str,
    row: tuple[Any, ...],
) -> None:
    conn.execute(
        "INSERT INTO session_insights (run_id, session_id, env, fetched_at,"
        " run_state, session_status, acus_consumed, billed_acus, consumption,"
        " session_size, category, num_user_messages, num_devin_messages,"
        " analysis_status, analysis, settled)"
        " VALUES (?, ?, 'sim', ?, ?, 'finished', ?, ?, ?, ?, ?, ?, ?, 'completed',"
        " ?, 1)",
        (run_id, session, at, outcome, *row),
    )
