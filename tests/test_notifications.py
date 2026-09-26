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
"""Formatting, suppression and delivery of Slack notifications."""

from __future__ import annotations

import sqlite3

from app.config import Settings
from app.devin_client import SessionSnapshot
from app.intake import Intake
from app.notifications import build_payload, escape, fingerprint, Kind
from app.slack_client import FakeSlackTransport
from app.states import State
from app.store import Store
from app.worker import Worker
from tests.conftest import deliver


def test_mentions_are_neutralised_and_markup_escaped() -> None:
    hostile = "<script> @channel please fix & ship"
    rendered = escape(hostile)
    assert "@channel" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&amp;" in rendered


def test_fingerprint_changes_with_the_revision() -> None:
    def fp(revision: str) -> str:
        return fingerprint(
            task_id=1, kind=Kind.PR_OPENED, destination="eng", revision=revision
        )

    assert fp("sha-a") != fp("sha-b")
    assert fp("sha-a") == fp("sha-a")


def test_payload_names_the_next_human_action(intake: Intake, store: Store) -> None:
    result = deliver(intake, "issues", "issue_labeled.json")
    task = store.get_task(int(result.task_id or 0))
    run = store.get_run(str(result.run_id))
    assert task is not None

    payload = build_payload(kind=Kind.PR_OPENED, task=task, run=run)
    text = payload["blocks"][0]["text"]["text"]
    assert "Next action:" in text
    assert "SoniaLei/superset-cognition-demo" in text
    assert "PR opened" in payload["text"]


def test_status_is_readable_from_the_emoji_alone(intake: Intake, store: Store) -> None:
    result = deliver(intake, "issues", "issue_labeled.json")
    task = store.get_task(int(result.task_id or 0))
    assert task is not None

    merged = build_payload(kind=Kind.PR_MERGED, task=task, run=None)
    closed = build_payload(kind=Kind.PR_CLOSED, task=task, run=None)
    blocked = build_payload(
        kind=Kind.NEEDS_HUMAN, task=task, run=None, reason="waiting_for_user"
    )
    operator = build_payload(
        kind=Kind.NEEDS_HUMAN, task=task, run=None, reason="capacity"
    )

    assert merged["text"].startswith(":white_check_mark:")
    assert closed["text"].startswith(":x:")
    assert blocked["text"].startswith(":warning:")
    # An out-of-credits org is not a stuck session and should not look like one.
    assert operator["text"].startswith(":rotating_light:")


def test_ready_for_review_does_not_claim_ci_passed(
    intake: Intake, store: Store
) -> None:
    result = deliver(intake, "issues", "issue_labeled.json")
    task = store.get_task(int(result.task_id or 0))
    assert task is not None
    payload = build_payload(kind=Kind.READY_FOR_REVIEW, task=task, run=None)
    text = payload["blocks"][0]["text"]["text"].lower()
    assert "verified" not in text
    assert "passed" not in text


def test_duplicate_logical_events_are_suppressed(intake: Intake, store: Store) -> None:
    result = deliver(intake, "issues", "issue_labeled.json")
    task_id = int(result.task_id or 0)
    task = store.get_task(task_id)
    assert task is not None
    payload = build_payload(kind=Kind.PR_OPENED, task=task, run=None)

    def enqueue(conn: sqlite3.Connection) -> bool:
        return store.enqueue_notification(
            conn,
            task_id=task_id,
            run_id=None,
            kind=Kind.PR_OPENED.value,
            reason=None,
            destination="engineering-updates",
            payload=payload,
            fingerprint=fingerprint(
                task_id=task_id,
                kind=Kind.PR_OPENED,
                destination="engineering-updates",
                revision="sha-a",
            ),
        )

    with store.transaction() as conn:
        assert enqueue(conn)
        assert not enqueue(conn)


def test_transient_slack_failures_are_retried_then_delivered(
    store: Store, settings: Settings
) -> None:
    slack = FakeSlackTransport(fail_times=1)
    worker = Worker(store, settings, _NullDevin(), slack, owner="test")
    intake = Intake(store, settings)
    deliver(intake, "issues", "issue_labeled.json")
    _enqueue(store, settings)

    assert worker.drain_outbox() == 0
    row = store.all_notifications()[-1]
    assert row["state"] == "pending"
    assert row["attempts"] == 1

    with store.transaction() as conn:
        conn.execute("UPDATE outbox SET next_attempt_at = NULL")
    assert worker.drain_outbox() == 1
    assert store.all_notifications()[-1]["state"] == "sent"
    assert len(slack.sent) == 1


def test_a_slack_failure_never_changes_the_run(
    store: Store, settings: Settings
) -> None:
    slack = FakeSlackTransport(fail_times=99)
    worker = Worker(store, settings, _NullDevin(), slack, owner="test")
    intake = Intake(store, settings)
    result = deliver(intake, "issues", "issue_labeled.json")
    with store.transaction() as conn:
        store.update_run(conn, str(result.run_id), state=State.PR_OPEN.value)
    _enqueue(store, settings)

    worker.drain_outbox()

    run = store.get_run(str(result.run_id))
    assert run is not None
    assert run["state"] == State.PR_OPEN.value


def test_an_unconfigured_destination_fails_the_message_not_the_run(
    store: Store, settings: Settings
) -> None:
    broken = Settings(
        github_webhook_secret=settings.github_webhook_secret,
        repo_allowlist=settings.repo_allowlist,
        maintainer_allowlist=settings.maintainer_allowlist,
        database_path=":memory:",
        slack_destinations={},
    )
    worker = Worker(store, broken, _NullDevin(), FakeSlackTransport(), owner="test")
    intake = Intake(store, broken)
    deliver(intake, "issues", "issue_labeled.json")
    _enqueue(store, broken)

    worker.drain_outbox()

    row = store.all_notifications()[-1]
    assert row["state"] == "failed"
    assert "engineering-updates" in str(row["last_error"])


def _enqueue(store: Store, settings: Settings) -> None:
    task = store.list_tasks()[0]
    task_id = int(task["id"])
    with store.transaction() as conn:
        store.enqueue_notification(
            conn,
            task_id=task_id,
            run_id=None,
            kind=Kind.PR_OPENED.value,
            reason=None,
            fingerprint=fingerprint(
                task_id=task_id,
                kind=Kind.PR_OPENED,
                destination=settings.default_destination,
                revision="sha-a",
            ),
            destination=settings.default_destination,
            payload=build_payload(kind=Kind.PR_OPENED, task=task, run=None),
        )


class _NullDevin:
    """A client that would fail loudly if the notification path called it."""

    def create_session(  # pragma: no cover
        self,
        *,
        prompt: str,
        title: str,
        repo_url: str,
        tags: list[str],
        max_acu_limit: int,
        playbook_id: str | None = None,
        secret_ids: list[str] | None = None,
    ) -> SessionSnapshot:
        raise AssertionError("notification delivery must not touch Devin")

    def get_session(self, session_id: str) -> SessionSnapshot:  # pragma: no cover
        raise AssertionError("notification delivery must not touch Devin")

    def find_session_by_tag(  # pragma: no cover
        self, tag: str
    ) -> SessionSnapshot | None:
        raise AssertionError("notification delivery must not touch Devin")

    def send_message(self, session_id: str, message: str) -> None:  # pragma: no cover
        raise AssertionError("notification delivery must not touch Devin")
