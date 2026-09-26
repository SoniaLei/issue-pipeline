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
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE runs SET {assignments} WHERE id = ?",
            (*fields.values(), run_id),
        )

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
