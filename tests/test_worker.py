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
"""The worker: session creation, polling, budgets and recovery."""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

import pytest

from app.config import Settings
from app.devin_client import (
    DevinError,
    PullRequestRef,
    SessionSnapshot,
    SimulatedDevinClient,
)
from app.intake import Intake
from app.slack_client import FakeSlackTransport
from app.states import State
from app.store import Store
from app.worker import Worker
from tests.conftest import deliver, load_script


def queued(intake: Intake) -> str:
    return str(deliver(intake, "issues", "issue_labeled.json").run_id)


def _run(store: Store, run_id: str) -> sqlite3.Row:
    run = store.get_run(run_id)
    assert run is not None
    return run


def test_start_creates_a_session_with_a_ceiling_and_tags(
    intake: Intake, store: Store, worker: Worker, devin: SimulatedDevinClient
) -> None:
    run_id = queued(intake)
    captured: dict[str, Any] = {}
    original = devin.create_session

    def spy(**kwargs: Any) -> SessionSnapshot:
        captured.update(kwargs)
        return original(**kwargs)

    devin.create_session = spy  # type: ignore[method-assign]
    worker.advance_one()

    assert captured["max_acu_limit"] == 20
    assert f"run:{run_id}" in captured["tags"]
    # Omitting secret_ids would grant the session every organization secret.
    assert captured["secret_ids"] == []
    assert run_id in captured["prompt"]

    run = store.get_run(run_id)
    assert run is not None
    assert run["session_id"] == "devin-sim-0001"
    assert run["branch"] == f"devin/issue-4242-{run_id}"
    assert run["state"] == State.STARTING.value


def test_polling_reaches_pr_open_and_records_acus(
    intake: Intake, store: Store, worker: Worker
) -> None:
    run_id = queued(intake)
    for _ in range(5):
        worker.advance_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.PR_OPEN.value
    assert run["pr_url"].endswith("/pull/91")
    assert run["acus_consumed"] == pytest.approx(4.25)
    # Provider vocabulary is preserved next to our own state, not instead of it.
    assert run["session_status"] == "running"
    assert run["session_status_detail"] == "finished"


def test_a_blocked_session_notifies_once(
    intake: Intake, store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    devin = SimulatedDevinClient(script=load_script("simulated_session_blocked.json"))
    worker = Worker(store, settings, devin, slack, owner="test")
    run_id = queued(Intake(store, settings))

    for _ in range(6):
        worker.advance_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.SESSION_BLOCKED.value
    assert run["failure_reason"] == "waiting_for_user"
    needs_human = [
        row for row in store.all_notifications() if row["kind"] == "needs_human"
    ]
    # Repeated polls of an unchanged state must not repeat the message.
    assert len(needs_human) == 1


def test_capacity_suspension_is_an_operator_problem(
    intake: Intake, store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    devin = SimulatedDevinClient(
        script=[
            {"status": "new"},
            {"status": "suspended", "status_detail": "out_of_credits"},
        ]
    )
    worker = Worker(store, settings, devin, slack, owner="test")
    queued(Intake(store, settings))

    for _ in range(4):
        worker.advance_one()

    notification = store.all_notifications()[-1]
    assert notification["reason"] == "capacity"
    # Routed away from the engineering channel: an org out of credits is not a
    # failed bug fix, and telling a maintainer it is wastes their review.
    assert notification["destination"] == "automation-alerts"


def test_hitting_the_acu_ceiling_fails_without_retry(
    store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    devin = SimulatedDevinClient(
        script=[
            {"status": "new"},
            {"status": "running", "status_detail": "working", "acus_consumed": 20.0},
        ]
    )
    worker = Worker(store, settings, devin, slack, owner="test")
    run_id = queued(Intake(store, settings))

    for _ in range(4):
        worker.advance_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.FAILED.value
    assert run["failure_reason"] == "acu_limit"


def test_concurrency_limit_holds_a_run_in_the_queue(
    store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    devin = SimulatedDevinClient(
        script=[{"status": "running", "status_detail": "working"}]
    )
    worker = Worker(store, settings, devin, slack, owner="test")
    intake = Intake(store, settings)

    run_ids = []
    for issue_number in (1, 2, 3):
        payload = _issue(issue_number)
        result = intake.handle(
            delivery_id=f"d-{issue_number}", event="issues", payload=payload
        )
        run_ids.append(str(result.run_id))
    for _ in range(3):
        worker.advance_one()

    runs = [_run(store, run_id) for run_id in run_ids]
    held = [run for run in runs if run["state"] == State.QUEUED.value]
    assert len(held) == 1
    # Queued, not failed: the approval it carries is still valid tomorrow.
    assert held[0]["failure_reason"] == "concurrency"


def test_an_ambiguous_create_is_reconciled_by_tag_not_retried(
    store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    """A create that times out may still have started a session."""

    class TimingOutClient(SimulatedDevinClient):
        def __init__(self) -> None:
            super().__init__(script=[{"status": "running", "status_detail": "working"}])
            self.create_calls = 0

        def create_session(self, **kwargs: Any) -> SessionSnapshot:
            self.create_calls += 1
            # The session is created, then the response is lost.
            super().create_session(**kwargs)
            raise DevinError("timeout calling /sessions", retryable=True)

    devin = TimingOutClient()
    worker = Worker(store, settings, devin, slack, owner="test")
    run_id = queued(Intake(store, settings))
    worker.advance_one()

    run = store.get_run(run_id)
    assert run is not None
    assert devin.create_calls == 1
    assert run["session_id"] == "devin-sim-0001"
    assert run["state"] == State.RUNNING.value


def test_an_expired_lease_is_reclaimed(store: Store, settings: Settings) -> None:
    intake = Intake(store, settings)
    run_id = queued(intake)

    first = store.claim_run("worker-a", lease_seconds=60)
    assert first is not None
    assert str(first["id"]) == run_id
    assert store.claim_run("worker-b", lease_seconds=60) is None

    with store.transaction() as conn:
        store.update_run(conn, run_id, lease_expires_at="2000-01-01T00:00:00+00:00")
    reclaimed = store.claim_run("worker-b", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed["lease_owner"] == "worker-b"


def test_more_than_one_pr_is_recorded_rather_than_forked(
    store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    devin = SimulatedDevinClient(
        script=[
            {"status": "new"},
            {
                "status": "running",
                "status_detail": "finished",
                "pull_requests": [
                    {"pr_url": "https://github.com/x/y/pull/1", "pr_state": "open"},
                    {"pr_url": "https://github.com/x/y/pull/2", "pr_state": "open"},
                ],
            },
        ]
    )
    worker = Worker(store, settings, devin, slack, owner="test")
    run_id = queued(Intake(store, settings))
    for _ in range(3):
        worker.advance_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["pr_url"].endswith("/pull/1")
    assert "pull/2" in str(run["extra_pr_urls"])


def test_snapshot_mapping_is_explicit() -> None:
    from app.devin_client import map_status

    base = SessionSnapshot(session_id="s", url="u", status="running")
    assert map_status(base, has_pr=False)[0] is State.RUNNING
    assert (
        map_status(dataclasses.replace(base, status_detail="waiting_for_user"), False)[
            0
        ]
        is State.SESSION_BLOCKED
    )
    # `finished` is a detail under `running`; reading only `status` never sees it.
    finished = dataclasses.replace(
        base,
        status_detail="finished",
        pull_requests=(PullRequestRef(url="https://example.invalid/pr/1"),),
    )
    assert map_status(finished, has_pr=True)[0] is State.PR_OPEN
    assert map_status(dataclasses.replace(base, status="error"), False) == (
        State.FAILED,
        "session_error",
    )


def _issue(number: int) -> dict[str, Any]:
    return {
        "action": "labeled",
        "issue": {
            "number": number,
            "title": f"issue {number}",
            "state": "open",
            "body": "repro steps",
            "labels": [{"name": "devin-ready"}],
        },
        "label": {"name": "devin-ready"},
        "repository": {"full_name": "SoniaLei/superset-cognition-demo"},
        "sender": {"login": "SoniaLei"},
    }
