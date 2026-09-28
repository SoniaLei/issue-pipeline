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
"""Durable state: deliveries, tasks, runs, leases and the notification outbox.

Two properties the rest of the service depends on:

* Every state change and the notifications it produces are written in one
  transaction, so a crash cannot leave a task that moved without the message
  that should have announced it.
* External calls never happen inside a transaction. The store records the
  intent; the worker performs the call afterwards and records the outcome.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.states import ACTIVE, State

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,          -- X-GitHub-Delivery
    event       TEXT NOT NULL,
    action      TEXT,
    repo        TEXT,
    received_at TEXT NOT NULL,
    accepted    INTEGER NOT NULL,          -- 0 recorded but not acted on
    reason      TEXT,                      -- why ignored or rejected
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY,
    repo         TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_title  TEXT NOT NULL DEFAULT '',
    issue_state  TEXT NOT NULL DEFAULT 'open',
    labels       TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (repo, issue_number)
);

CREATE TABLE IF NOT EXISTS runs (
    id                    TEXT PRIMARY KEY,   -- run ID, appears in branch and PR marker
    task_id               INTEGER NOT NULL REFERENCES tasks(id),
    state                 TEXT NOT NULL,
    env                   TEXT NOT NULL,      -- live | sim, never mixed in a report

    approved_by           TEXT,
    approved_at           TEXT,
    approval_revoked_by   TEXT,
    approval_revoked_at   TEXT,
    input_snapshot        TEXT,               -- issue as it was at approval

    lease_owner           TEXT,
    lease_expires_at      TEXT,

    session_id            TEXT,
    session_url           TEXT,
    session_status        TEXT,               -- provider status, verbatim
    session_status_detail TEXT,               -- provider detail, verbatim
    session_polled_at     TEXT,
    session_finished_at   TEXT,
    acus_consumed         REAL,
    max_acu_limit         INTEGER,
    structured_output     TEXT,

    branch                TEXT,
    base_sha              TEXT,
    pr_number             INTEGER,
    pr_url                TEXT,
    pr_state              TEXT,
    pr_draft              INTEGER,
    head_sha              TEXT,
    checks_state          TEXT,
    checks_head_sha       TEXT,
    review_state          TEXT,
    merged_sha            TEXT,
    extra_pr_urls         TEXT NOT NULL DEFAULT '[]',

    failure_reason        TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

-- At most one active run per issue. Partial, so historical attempts do not
-- collide with a new one.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active
    ON runs (task_id) WHERE state IN ({active});

CREATE UNIQUE INDEX IF NOT EXISTS runs_one_pr
    ON runs (task_id, pr_number) WHERE pr_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL REFERENCES tasks(id),
    run_id          TEXT REFERENCES runs(id),
    kind            TEXT NOT NULL,
    reason          TEXT,
    -- Over (task, kind, destination, relevant revision/state): a new head SHA
    -- is a new message, an unchanged poll is not.
    fingerprint     TEXT NOT NULL UNIQUE,
    destination     TEXT NOT NULL,
    payload         TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_response   TEXT,
    next_attempt_at TEXT,
    created_at      TEXT NOT NULL,
    sent_at         TEXT
);

CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (state, next_attempt_at);

-- Append-only history of what happened to a run: every state change, plus
-- the observations (session, PR, checks, review) that a timeline needs.
CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- state|session|pr|checks|verified|
                                           -- review|review_gate|insights
    from_state  TEXT,
    to_state    TEXT,
    reason      TEXT,
    detail      TEXT                       -- JSON, kind-specific
);

CREATE INDEX IF NOT EXISTS run_events_run ON run_events (run_id, id);

-- One row per check suite GitHub reported for a head SHA on a tracked run.
-- Verification is derived from all suites on the *current* head, never
-- from a single one.
CREATE TABLE IF NOT EXISTS checks (
    run_id      TEXT NOT NULL REFERENCES runs(id),
    head_sha    TEXT NOT NULL,
    suite_id    TEXT NOT NULL,
    app         TEXT,
    status      TEXT NOT NULL,             -- queued | in_progress | completed
    conclusion  TEXT,                      -- success | failure | ... | NULL
    url         TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, head_sha, suite_id)
);

-- Check suites keyed by repository head, whether or not a run tracks that
-- head yet. GitHub reports suites for a pushed commit before the pull request
-- that carries it is opened; a run that only learns about suites after its PR
-- exists would see the first completed one arrive alone and call it a pass.
CREATE TABLE IF NOT EXISTS head_checks (
    repo        TEXT NOT NULL,
    head_sha    TEXT NOT NULL,
    suite_id    TEXT NOT NULL,
    app         TEXT,
    status      TEXT NOT NULL,
    conclusion  TEXT,
    url         TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (repo, head_sha, suite_id)
);

-- Liveness of the processes that are supposed to be running. A dashboard
-- that cannot tell "nothing happened" from "nobody is looking" is useless.
CREATE TABLE IF NOT EXISTS heartbeats (
    component   TEXT PRIMARY KEY,          -- worker | api
    owner       TEXT,
    at          TEXT NOT NULL,
    detail      TEXT
);

-- Devin's account of one run's session: cost, size, and its own analysis of
-- what went well or badly. Kept in its own table because it is provider
-- testimony, refreshed on a schedule, and none of it changes a run's state.
CREATE TABLE IF NOT EXISTS session_insights (
    run_id              TEXT PRIMARY KEY REFERENCES runs(id),
    session_id          TEXT NOT NULL,
    env                 TEXT NOT NULL,
    fetched_at          TEXT NOT NULL,
    fetch_count         INTEGER NOT NULL DEFAULT 1,
    run_state           TEXT,                -- run state at fetch, drives refresh
    session_status      TEXT,
    session_status_detail TEXT,
    acus_consumed       REAL,                -- session's running total
    billed_acus         REAL,                -- daily consumption, billing-grade
    consumption         TEXT NOT NULL DEFAULT '[]',   -- JSON [[day, acus], ...]
    session_size        TEXT,                -- xs | s | m | l | xl
    category            TEXT,
    subcategory         TEXT,
    origin              TEXT,
    service_user_id     TEXT,
    num_user_messages   INTEGER,
    num_devin_messages  INTEGER,
    analysis_status     TEXT,                -- started | completed | failed
    analysis            TEXT,                -- JSON, verbatim
    generate_requested_at TEXT,
    settled             INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT
);

-- Devin Review of one PR head (D-033). One row per (run, commit): a new head
-- gets a new row and the old one stays as history, so a verdict can never be
-- read against a commit it was not given for. `status` is what the pr-reviews
-- API says about progress; `findings` is what GitHub carries as the bot's
-- review of that commit. Only the latter can clear a head.
CREATE TABLE IF NOT EXISTS pr_reviews (
    run_id          TEXT NOT NULL REFERENCES runs(id),
    head_sha        TEXT NOT NULL,
    env             TEXT NOT NULL,
    pr_url          TEXT NOT NULL,
    status          TEXT,                    -- pending|running|completed|errored|
                                             -- cancelled|skipped|unavailable
    requested_at    TEXT,                    -- when this worker asked for it
    status_at       TEXT,                    -- last API read
    attempts        INTEGER NOT NULL DEFAULT 0,  -- failed API calls in a row
    retryable       INTEGER NOT NULL DEFAULT 1,  -- 0: last error was permanent
    last_error      TEXT,
    findings        INTEGER,                 -- from the bot's GitHub review summary
    findings_by_kind TEXT NOT NULL DEFAULT '{{}}',  -- JSON kind -> count
    review_url      TEXT,                    -- the bot's review on GitHub
    verdict_at      TEXT,
    PRIMARY KEY (run_id, head_sha)
);

-- The latest organization-level counts Devin reports for the pipeline's own
-- service user, kept per environment so the drift check against the store
-- compares like with like.
CREATE TABLE IF NOT EXISTS provider_metrics (
    env                 TEXT PRIMARY KEY,
    attempted_at        TEXT NOT NULL,       -- last read, successful or not
    fetched_at          TEXT,                -- last successful read; window,
    window_after        TEXT,                -- ids and metrics below belong
    window_before       TEXT,                -- to it and move together
    service_user_ids    TEXT,                -- JSON list
    metrics             TEXT,                -- JSON, verbatim
    last_error          TEXT
);
""".format(active=", ".join(f"'{value}'" for value in sorted(s.value for s in ACTIVE)))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat()


def new_run_id() -> str:
    """Short, human-quotable, and unique enough to live in a branch name."""
    return uuid.uuid4().hex[:12]


class Store:
    """SQLite-backed persistence.

    SQLite is deliberate for a single host: one file, one volume, real
    transactions. The boundary at which it stops being the right answer is
    multiple hosts, which is also the boundary at which the lease table stops
    being enough.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One unit of work. No external call may happen inside this block."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ----------------------------------------------------------------- deliveries

    def delivery_seen(self, delivery_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        return row is not None

    def record_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        delivery_id: str,
        event: str,
        action: str | None,
        repo: str | None,
        payload: dict[str, Any],
        accepted: bool,
        reason: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO deliveries
                (delivery_id, event, action, repo, received_at, accepted,
                 reason, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                event,
                action,
                repo,
                now_iso(),
                int(accepted),
                reason,
                json.dumps(payload),
            ),
        )

    def prune_deliveries(self, retention_days: int) -> int:
        cutoff = (utcnow() - timedelta(days=retention_days)).isoformat()
        cursor = self._conn.execute(
            "DELETE FROM deliveries WHERE received_at < ?", (cutoff,)
        )
        return cursor.rowcount

    # ---------------------------------------------------------------------- tasks

    def upsert_task(
        self,
        conn: sqlite3.Connection,
        *,
        repo: str,
        issue_number: int,
        title: str,
        issue_state: str,
        labels: list[str],
    ) -> int:
        conn.execute(
            """
            INSERT INTO tasks (repo, issue_number, issue_title, issue_state, labels,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (repo, issue_number) DO UPDATE SET
                issue_title = excluded.issue_title,
                issue_state = excluded.issue_state,
                labels      = excluded.labels,
                updated_at  = excluded.updated_at
            """,
            (
                repo,
                issue_number,
                title,
                issue_state,
                json.dumps(sorted(labels)),
                now_iso(),
                now_iso(),
            ),
        )
        row = conn.execute(
            "SELECT id FROM tasks WHERE repo = ? AND issue_number = ?",
            (repo, issue_number),
        ).fetchone()
        return int(row["id"])

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    def find_task(self, repo: str, issue_number: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM tasks WHERE repo = ? AND issue_number = ?",
            (repo, issue_number),
        ).fetchone()

    def list_tasks(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC"
            ).fetchall()
        )

    # ----------------------------------------------------------------------- runs

    def create_run(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
        state: State,
        env: str,
        approved_by: str | None = None,
        approved_at: str | None = None,
        input_snapshot: dict[str, Any] | None = None,
        max_acu_limit: int | None = None,
    ) -> str:
        run_id = new_run_id()
        conn.execute(
            """
            INSERT INTO runs (id, task_id, state, env, approved_by, approved_at,
                              input_snapshot, max_acu_limit, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                task_id,
                state.value,
                env,
                approved_by,
                approved_at,
                json.dumps(input_snapshot) if input_snapshot is not None else None,
                max_acu_limit,
                now_iso(),
                now_iso(),
            ),
        )
        self.record_event(
            conn,
            run_id=run_id,
            task_id=task_id,
            kind="state",
            to_state=state.value,
            detail={"approved_by": approved_by} if approved_by else None,
        )
        return run_id

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()

    def active_run_for_task(self, task_id: int) -> sqlite3.Row | None:
        placeholders = ", ".join("?" for _ in ACTIVE)
        return self._conn.execute(
            f"SELECT * FROM runs WHERE task_id = ? AND state IN ({placeholders})",
            (task_id, *sorted(s.value for s in ACTIVE)),
        ).fetchone()

    def latest_run_for_task(self, task_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    def runs_for_task(self, task_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at DESC",
                (task_id,),
            ).fetchall()
        )

    def update_run(self, conn: sqlite3.Connection, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        previous: sqlite3.Row | None = None
        if "state" in fields:
            previous = conn.execute(
                "SELECT task_id, state FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE runs SET {assignments} WHERE id = ?",
            (*fields.values(), run_id),
        )
        if previous is not None and previous["state"] != fields["state"]:
            self.record_event(
                conn,
                run_id=run_id,
                task_id=int(previous["task_id"]),
                kind="state",
                from_state=str(previous["state"]),
                to_state=str(fields["state"]),
                reason=fields.get("failure_reason"),
            )

    # --------------------------------------------------------------------- events

    def record_event(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        task_id: int,
        kind: str,
        from_state: str | None = None,
        to_state: str | None = None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO run_events
                (run_id, task_id, at, kind, from_state, to_state, reason, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                task_id,
                now_iso(),
                kind,
                from_state,
                to_state,
                reason,
                json.dumps(detail) if detail is not None else None,
            ),
        )

    def events_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        )

    def events_for_task(self, task_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM run_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        )

    def all_events(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute("SELECT * FROM run_events ORDER BY id").fetchall()
        )

    # --------------------------------------------------------------------- checks

    def record_check_suite(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        head_sha: str,
        suite_id: str,
        app: str | None,
        status: str,
        conclusion: str | None,
        url: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO checks
                (run_id, head_sha, suite_id, app, status, conclusion, url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, head_sha, suite_id) DO UPDATE SET
                app = excluded.app,
                status = excluded.status,
                conclusion = excluded.conclusion,
                url = excluded.url,
                updated_at = excluded.updated_at
            """,
            (run_id, head_sha, suite_id, app, status, conclusion, url, now_iso()),
        )

    def record_head_check_suite(
        self,
        conn: sqlite3.Connection,
        *,
        repo: str,
        head_sha: str,
        suite_id: str,
        app: str | None,
        status: str,
        conclusion: str | None,
        url: str | None,
        retention: timedelta = timedelta(days=14),
    ) -> None:
        conn.execute(
            "DELETE FROM head_checks WHERE updated_at < ?",
            ((utcnow() - retention).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO head_checks
                (repo, head_sha, suite_id, app, status, conclusion, url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (repo, head_sha, suite_id) DO UPDATE SET
                app = excluded.app,
                status = excluded.status,
                conclusion = excluded.conclusion,
                url = excluded.url,
                updated_at = excluded.updated_at
            """,
            (repo, head_sha, suite_id, app, status, conclusion, url, now_iso()),
        )

    def adopt_head_checks(
        self, conn: sqlite3.Connection, *, run_id: str, repo: str, head_sha: str
    ) -> int:
        """Copy every suite already seen for this head onto the run."""
        cursor = conn.execute(
            """
            INSERT INTO checks
                (run_id, head_sha, suite_id, app, status, conclusion, url, updated_at)
            SELECT ?, head_sha, suite_id, app, status, conclusion, url, updated_at
            FROM head_checks WHERE repo = ? AND head_sha = ?
            ON CONFLICT (run_id, head_sha, suite_id) DO UPDATE SET
                app = excluded.app,
                status = excluded.status,
                conclusion = excluded.conclusion,
                url = excluded.url,
                updated_at = excluded.updated_at
            """,
            (run_id, repo, head_sha),
        )
        return int(cursor.rowcount or 0)

    def checks_for_head(self, run_id: str, head_sha: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM checks WHERE run_id = ? AND head_sha = ? ORDER BY app",
                (run_id, head_sha),
            ).fetchall()
        )

    def runs_with_head(self, repo: str, head_sha: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT runs.* FROM runs
                JOIN tasks ON tasks.id = runs.task_id
                WHERE tasks.repo = ? AND runs.head_sha = ?
                """,
                (repo, head_sha),
            ).fetchall()
        )

    # ----------------------------------------------------------------- heartbeats

    def heartbeat(self, component: str, owner: str, detail: str | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO heartbeats (component, owner, at, detail)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (component) DO UPDATE SET
                owner = excluded.owner, at = excluded.at, detail = excluded.detail
            """,
            (component, owner, now_iso(), detail),
        )

    def heartbeats(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute("SELECT * FROM heartbeats ORDER BY component").fetchall()
        )

    # ------------------------------------------------------------------ analytics

    def insights_candidates(
        self, env: str, refresh_before: datetime
    ) -> list[sqlite3.Row]:
        """Runs whose provider analysis is missing, stale, or behind the run.

        A run is refreshed when it has never been read, when its state or the
        session's status moved since the last read, or when the last read is
        older than the refresh interval; a settled row is never refreshed.
        """
        return list(
            self._conn.execute(
                """
                SELECT runs.*, tasks.repo AS task_repo, tasks.issue_number,
                       tasks.issue_title, tasks.issue_state,
                       si.fetched_at AS insights_fetched_at
                FROM runs JOIN tasks ON tasks.id = runs.task_id
                LEFT JOIN session_insights si ON si.run_id = runs.id
                WHERE runs.env = ? AND runs.session_id IS NOT NULL
                  AND (si.run_id IS NULL
                       OR (si.settled = 0
                           AND (si.run_state IS NOT runs.state
                                OR si.session_status IS NOT runs.session_status
                                OR si.session_status_detail
                                   IS NOT runs.session_status_detail
                                OR si.fetched_at < ?)))
                ORDER BY si.fetched_at IS NOT NULL, si.fetched_at, runs.created_at
                """,
                (env, refresh_before.isoformat()),
            ).fetchall()
        )

    def upsert_session_insights(
        self, conn: sqlite3.Connection, run_id: str, **fields: Any
    ) -> None:
        existing = conn.execute(
            "SELECT fetch_count FROM session_insights WHERE run_id = ?", (run_id,)
        ).fetchone()
        fields["fetched_at"] = now_iso()
        if existing is None:
            columns = ["run_id", *fields]
            conn.execute(
                f"INSERT INTO session_insights ({', '.join(columns)})"
                f" VALUES ({', '.join('?' for _ in columns)})",
                (run_id, *fields.values()),
            )
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE session_insights SET {assignments},"
            " fetch_count = fetch_count + 1 WHERE run_id = ?",
            (*fields.values(), run_id),
        )

    def insights_for_run(self, run_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM session_insights WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row

    def insights_for_env(self, env: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM session_insights WHERE env = ? ORDER BY fetched_at",
                (env,),
            ).fetchall()
        )

    def save_provider_metrics(
        self,
        env: str,
        *,
        window_after: datetime,
        window_before: datetime,
        service_user_ids: list[str],
        metrics: dict[str, Any] | None,
        error: str | None = None,
    ) -> None:
        ok = metrics is not None
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO provider_metrics
                    (env, attempted_at, fetched_at, window_after, window_before,
                     service_user_ids, metrics, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (env) DO UPDATE SET
                    attempted_at = excluded.attempted_at,
                    -- A failed refresh keeps the last good numbers together
                    -- with the window and identities they were read for.
                    fetched_at = COALESCE(
                        excluded.fetched_at, provider_metrics.fetched_at),
                    window_after = COALESCE(
                        excluded.window_after, provider_metrics.window_after),
                    window_before = COALESCE(
                        excluded.window_before, provider_metrics.window_before),
                    service_user_ids = COALESCE(
                        excluded.service_user_ids, provider_metrics.service_user_ids),
                    metrics = COALESCE(excluded.metrics, provider_metrics.metrics),
                    last_error = excluded.last_error
                """,
                (
                    env,
                    now_iso(),
                    now_iso() if ok else None,
                    window_after.isoformat() if ok else None,
                    window_before.isoformat() if ok else None,
                    json.dumps(service_user_ids) if ok else None,
                    json.dumps(metrics) if ok else None,
                    error,
                ),
            )

    def provider_metrics(self, env: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM provider_metrics WHERE env = ?", (env,)
        ).fetchone()
        return row

    # ------------------------------------------------------------- review gate

    def review_for_head(self, run_id: str, head_sha: str | None) -> sqlite3.Row | None:
        if not head_sha:
            return None
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM pr_reviews WHERE run_id = ? AND head_sha = ?",
            (run_id, head_sha),
        ).fetchone()
        return row

    def reviews_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM pr_reviews WHERE run_id = ?"
                " ORDER BY COALESCE(requested_at, verdict_at, status_at)",
                (run_id,),
            ).fetchall()
        )

    def upsert_review(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        head_sha: str,
        env: str,
        pr_url: str,
        **fields: Any,
    ) -> None:
        """Insert or update the review row for one head. Fields not given keep
        their value, so a GitHub verdict and an API status written by
        different code paths land on the same row without overwriting each
        other."""
        conn.execute(
            "INSERT OR IGNORE INTO pr_reviews (run_id, head_sha, env, pr_url)"
            " VALUES (?, ?, ?, ?)",
            (run_id, head_sha, env, pr_url),
        )
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE pr_reviews SET {assignments} WHERE run_id = ? AND head_sha = ?",
            (*fields.values(), run_id, head_sha),
        )

    def claim_review_request(
        self, *, run_id: str, head_sha: str, env: str, pr_url: str, requested_at: str
    ) -> bool:
        """Mark this head as requested, in its own transaction, before the
        request leaves the process. Returns False when another worker already
        holds the claim, so exactly one review is asked for per head."""
        with self.transaction() as conn:
            self.upsert_review(
                conn, run_id=run_id, head_sha=head_sha, env=env, pr_url=pr_url
            )
            cursor = conn.execute(
                "UPDATE pr_reviews SET requested_at = ?"
                " WHERE run_id = ? AND head_sha = ? AND requested_at IS NULL",
                (requested_at, run_id, head_sha),
            )
            return cursor.rowcount == 1

    def release_review_request(self, run_id: str, head_sha: str) -> None:
        """Undo a claim whose request failed before Devin accepted it, so the
        next read may ask again."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE pr_reviews SET requested_at = NULL"
                " WHERE run_id = ? AND head_sha = ?",
                (run_id, head_sha),
            )

    def review_candidates(self, env: str) -> list[sqlite3.Row]:
        """Runs whose current PR head still needs a review-gate read: GitHub
        owns the run (pr_open / awaiting_review) and the head has no row, or a
        row whose API status is not terminal, and GitHub has not already
        delivered the bot's verdict for it. Heads left ``unavailable`` by a
        permanent error (``retryable = 0``) are not read again. Oldest read
        first."""
        return list(
            self._conn.execute(
                """
                SELECT runs.*, tasks.repo AS task_repo, tasks.issue_number,
                       pr_reviews.status AS review_status,
                       pr_reviews.status_at AS review_status_at,
                       pr_reviews.attempts AS review_attempts,
                       pr_reviews.findings AS review_findings
                FROM runs
                JOIN tasks ON tasks.id = runs.task_id
                LEFT JOIN pr_reviews
                  ON pr_reviews.run_id = runs.id AND pr_reviews.head_sha = runs.head_sha
                WHERE runs.env = ?
                  AND runs.state IN ('pr_open', 'awaiting_review')
                  AND runs.pr_url IS NOT NULL AND runs.head_sha IS NOT NULL
                  AND pr_reviews.findings IS NULL
                  AND (pr_reviews.status IS NULL
                       OR pr_reviews.status IN ('pending', 'running')
                       OR (pr_reviews.status = 'unavailable'
                           AND pr_reviews.retryable = 1))
                ORDER BY pr_reviews.status_at IS NOT NULL, pr_reviews.status_at
                """,
                (env,),
            ).fetchall()
        )

    def last_review_call(self, env: str) -> sqlite3.Row | None:
        """Most recent pr-reviews API read in this environment, for health."""
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT status, status_at, last_error FROM pr_reviews"
            " WHERE env = ? AND status_at IS NOT NULL ORDER BY status_at DESC LIMIT 1",
            (env,),
        ).fetchone()
        return row

    def service_user_ids(self, env: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT service_user_id FROM session_insights"
            " WHERE env = ? AND service_user_id IS NOT NULL ORDER BY 1",
            (env,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def earliest_run_created_at(self, env: str) -> str | None:
        row = self._conn.execute(
            "SELECT MIN(created_at) AS at FROM runs WHERE env = ?", (env,)
        ).fetchone()
        return str(row["at"]) if row and row["at"] else None

    # ------------------------------------------------------------------ overview

    def list_runs(self, env: str | None = None) -> list[sqlite3.Row]:
        """Every run joined to its task, optionally for one environment only.

        The environment filter is here rather than in the caller so a report
        cannot accidentally sum live and simulated runs together.
        """
        sql = """
            SELECT runs.*, tasks.repo AS task_repo, tasks.issue_number,
                   tasks.issue_title, tasks.issue_state
            FROM runs JOIN tasks ON tasks.id = runs.task_id
        """
        params: tuple[Any, ...] = ()
        if env is not None:
            sql += " WHERE runs.env = ?"
            params = (env,)
        sql += " ORDER BY runs.updated_at DESC"
        return list(self._conn.execute(sql, params).fetchall())

    def count_runs_by_env(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT env, COUNT(*) AS n FROM runs GROUP BY env"
        ).fetchall()
        return {str(row["env"]): int(row["n"]) for row in rows}

    def last_delivery_at(self) -> str | None:
        row = self._conn.execute(
            "SELECT MAX(received_at) AS at FROM deliveries"
        ).fetchone()
        return str(row["at"]) if row and row["at"] else None

    def last_poll_at(self, env: str) -> str | None:
        row = self._conn.execute(
            "SELECT MAX(session_polled_at) AS at FROM runs WHERE env = ?", (env,)
        ).fetchone()
        return str(row["at"]) if row and row["at"] else None

    def find_run_by_branch(self, repo: str, branch: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT runs.* FROM runs
            JOIN tasks ON tasks.id = runs.task_id
            WHERE tasks.repo = ? AND runs.branch = ?
            ORDER BY runs.created_at DESC LIMIT 1
            """,
            (repo, branch),
        ).fetchone()

    def find_run_in_repo(self, repo: str, run_id: str) -> sqlite3.Row | None:
        """Look up a run by ID *within a repository*.

        The repository is part of the lookup on purpose: a run marker in a PR
        body is public text that anyone can copy, so it only ever identifies a
        run within the repository the run was authorized for.
        """
        return self._conn.execute(
            """
            SELECT runs.* FROM runs
            JOIN tasks ON tasks.id = runs.task_id
            WHERE tasks.repo = ? AND runs.id = ?
            """,
            (repo, run_id),
        ).fetchone()

    def count_active_runs(self, repo: str) -> int:
        placeholders = ", ".join("?" for _ in ACTIVE)
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM runs
            JOIN tasks ON tasks.id = runs.task_id
            WHERE tasks.repo = ? AND runs.state IN ({placeholders})
              AND runs.session_id IS NOT NULL
            """,
            (repo, *sorted(s.value for s in ACTIVE)),
        ).fetchone()
        return int(row["n"])

    def count_sessions_started_since(self, repo: str, since: datetime) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM runs
            JOIN tasks ON tasks.id = runs.task_id
            WHERE tasks.repo = ? AND runs.session_id IS NOT NULL
              AND runs.created_at >= ?
            """,
            (repo, since.isoformat()),
        ).fetchone()
        return int(row["n"])

    # --------------------------------------------------------------------- leases

    def claim_run(self, owner: str, lease_seconds: int) -> sqlite3.Row | None:
        """Claim one queued or in-flight run, reclaiming expired leases.

        A lease that has expired is assumed abandoned rather than finished: a
        worker that died mid-run left the row exactly as it was, and the only
        way the task ever moves again is for someone to pick it back up.
        """
        now = utcnow()
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        claimable = (
            State.QUEUED.value,
            State.STARTING.value,
            State.RUNNING.value,
            State.SESSION_BLOCKED.value,
        )
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE state IN (?, ?, ?, ?)
                  AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                -- Unstarted work first, then least recently touched. Ordering
                -- by age alone lets one long-running session be re-polled
                -- forever while approved runs never start.
                ORDER BY CASE state WHEN 'queued' THEN 0 ELSE 1 END, updated_at
                LIMIT 1
                """,
                (*claimable, now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE runs SET lease_owner = ?, lease_expires_at = ?, updated_at = ?"
                " WHERE id = ?",
                (owner, expiry, now_iso(), row["id"]),
            )
        return self.get_run(str(row["id"]))

    def release_run(self, run_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL,"
                " updated_at = ? WHERE id = ?",
                (now_iso(), run_id),
            )

    # --------------------------------------------------------------------- outbox

    def enqueue_notification(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
        run_id: str | None,
        kind: str,
        reason: str | None,
        fingerprint: str,
        destination: str,
        payload: dict[str, Any],
    ) -> bool:
        """Queue a notification. Returns False when the fingerprint is a repeat.

        The uniqueness of the fingerprint is what stops a task being announced
        twice, and it is enforced by the database rather than by a check the
        caller might forget.
        """
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO outbox
                (task_id, run_id, kind, reason, fingerprint, destination, payload,
                 state, next_attempt_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                task_id,
                run_id,
                kind,
                reason,
                fingerprint,
                destination,
                json.dumps(payload),
                now_iso(),
                now_iso(),
            ),
        )
        return cursor.rowcount > 0

    def due_notifications(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT * FROM outbox
                WHERE state = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id LIMIT ?
                """,
                (now_iso(), limit),
            ).fetchall()
        )

    def mark_notification_sent(self, outbox_id: int, response: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET state = 'sent', attempts = attempts + 1, sent_at = ?,
                    last_response = ?, last_error = NULL
                WHERE id = ?
                """,
                (now_iso(), response[:500], outbox_id),
            )

    def mark_notification_retry(
        self, outbox_id: int, error: str, retry_after_seconds: float
    ) -> None:
        next_at = (utcnow() + timedelta(seconds=retry_after_seconds)).isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?
                WHERE id = ?
                """,
                (error[:500], next_at, outbox_id),
            )

    def mark_notification_failed(self, outbox_id: int, error: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET state = 'failed', attempts = attempts + 1, last_error = ?
                WHERE id = ?
                """,
                (error[:500], outbox_id),
            )

    def notifications_for_task(self, task_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM outbox WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        )

    def all_notifications(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM outbox ORDER BY id").fetchall())
