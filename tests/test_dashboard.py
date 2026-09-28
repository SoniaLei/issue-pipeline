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
"""The progress dashboard: outcome definitions, env isolation, timelines."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dashboard import build_dashboard, build_timeline, is_verified
from app.intake import Intake
from app.main import create_app
from app.states import State
from app.store import Store
from app.worker import Worker
from tests.conftest import (
    deliver,
    load_fixture,
    next_delivery_id,
    substitute_run_id,
)
from tests.test_correlation import deliver_pr, queued_run

REPO = "SoniaLei/superset-cognition-demo"


def check_suite(
    run_id: str, *, head_sha: str, conclusion: str | None, status: str = "completed"
) -> dict[str, Any]:
    return {
        "action": status,
        "check_suite": {
            "status": status,
            "conclusion": conclusion,
            "head_sha": head_sha,
            "pull_requests": [{"number": 91}],
        },
        "repository": {"full_name": REPO},
        "sender": {"login": "github-actions[bot]"},
    }


def deliver_checks(intake: Intake, run_id: str, **kwargs: Any) -> Any:
    return intake.handle(
        delivery_id=next_delivery_id(),
        event="check_suite",
        payload=check_suite(run_id, **kwargs),
    )


def run_row(store: Store, run_id: str) -> Any:
    run = store.get_run(run_id)
    assert run is not None
    return run


# ------------------------------------------------------------- verification


def test_pr_opened_is_not_verified(intake: Intake, store: Store) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    run = run_row(store, run_id)
    assert run["pr_url"] is not None
    assert not is_verified(run)
    data = build_dashboard(store, "sim")
    assert data["results"] == {
        "pr_opened": 1,
        "verified": 0,
        "merged": 0,
        "closed_unmerged": 0,
    }


def test_successful_checks_on_the_current_head_verify(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    result = deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="success")
    assert result.accepted
    run = run_row(store, run_id)
    assert run["state"] == State.AWAITING_REVIEW.value
    assert is_verified(run)
    assert build_dashboard(store, "sim")["results"]["verified"] == 1
    assert [n["kind"] for n in store.all_notifications()] == ["pr_opened", "verified"]


def test_checks_for_a_superseded_head_do_not_verify(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    payload = substitute_run_id(load_fixture("pr_opened.json"), run_id)
    payload["action"] = "synchronize"
    payload["pull_request"]["head"]["sha"] = "99887766"
    intake.handle(delivery_id=next_delivery_id(), event="pull_request", payload=payload)

    result = deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="success")
    assert result.accepted
    run = run_row(store, run_id)
    assert run["state"] == State.PR_OPEN.value
    assert run["checks_state"] is None
    assert not is_verified(run)
    kinds = [(e["kind"], e["detail"]) for e in store.events_for_run(run_id)]
    assert ("checks", "stale head a1b2c3d") in kinds


def test_failed_checks_take_a_verified_run_back_to_pr_open(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="success")
    deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="failure")
    run = run_row(store, run_id)
    assert run["state"] == State.PR_OPEN.value
    assert run["checks_state"] == "failure"
    assert not is_verified(run)


def test_a_pending_suite_is_recorded_but_changes_nothing(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    deliver_checks(
        intake, run_id, head_sha="a1b2c3d4", conclusion=None, status="requested"
    )
    run = run_row(store, run_id)
    assert run["state"] == State.PR_OPEN.value
    assert not is_verified(run)


def test_merged_requires_github_confirmation(intake: Intake, store: Store) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="success")
    deliver_pr(intake, "pr_merged.json", run_id)
    data = build_dashboard(store, "sim")
    assert data["results"]["merged"] == 1
    assert data["workload"] == {
        "queued": 0,
        "running": 0,
        "awaiting_review": 0,
        "blocked": 0,
        "failed": 0,
    }
    assert data["throughput"][-1]["verified"] == 1
    assert data["throughput"][-1]["merged"] == 1
    assert data["speed"]["sample_count"] == 1
    assert data["speed"]["median_seconds_to_review_ready"] >= 0


def test_reverifying_the_same_pr_counts_once_in_throughput(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="success")
    deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="failure")
    deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="success")
    data = build_dashboard(store, "sim")
    assert data["results"]["verified"] == 1
    assert sum(day["verified"] for day in data["throughput"]) == 1


# ------------------------------------------------------------- workload/attention


def test_blocked_and_failed_runs_carry_reason_and_next_action(
    intake: Intake, store: Store
) -> None:
    blocked = queued_run(intake, store)
    with store.transaction() as conn:
        store.update_run(
            conn,
            blocked,
            state=State.SESSION_BLOCKED.value,
            failure_reason="session asked for credentials",
        )
    data = build_dashboard(store, "sim")
    assert data["workload"]["blocked"] == 1
    (item,) = data["attention"]
    assert item["run_id"] == blocked
    assert item["blocker"] == "session asked for credentials"
    assert item["next_action"]
    assert item["age_seconds"] is not None
    row = data["tasks"][0]
    assert row["failure_reason"] == "session asked for credentials"
    assert row["next_action"] == item["next_action"]


def test_task_rows_have_the_requested_columns(intake: Intake, store: Store) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    (row,) = build_dashboard(store, "sim")["tasks"]
    assert row["run_id"] == run_id
    assert row["issue"]["number"] == 4242
    assert row["state"] == State.PR_OPEN.value
    assert row["elapsed_seconds"] is not None
    assert row["tests"]["checks"] == "unknown"
    assert row["pr"]["url"].endswith("/pull/91")
    assert row["slack"]["summary"] == "pending"
    assert row["last_update"] is not None


# ------------------------------------------------------------- env isolation


def test_live_metrics_never_include_simulation_runs(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="success")
    assert run_row(store, run_id)["env"] == "sim"

    live = build_dashboard(store, "live")
    assert live["env"] == "live"
    assert live["tasks"] == []
    assert live["results"]["verified"] == 0
    assert live["speed"]["sample_count"] == 0
    assert live["throughput"] == []
    assert live["health"]["slack"]["failed_count"] == 0

    sim = build_dashboard(store, "sim")
    assert sim["results"]["verified"] == 1
    assert sim["generated_at"]


def test_slack_health_is_scoped_to_the_environment(
    intake: Intake, store: Store, worker: Worker
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    assert worker.drain_outbox() == 1
    sim = build_dashboard(store, "sim")["health"]
    live = build_dashboard(store, "live")["health"]
    assert sim["slack"]["last_successful_delivery"] is not None
    assert live["slack"]["last_successful_delivery"] is None
    # Deliveries carry no environment; the figure says so rather than pretend.
    assert live["github"]["scope"] == "all environments"
    assert live["github"]["last_webhook_received"] is not None


def test_an_unknown_env_is_refused(store: Store) -> None:
    with pytest.raises(ValueError, match="env must be one of"):
        build_dashboard(store, "all")


# ------------------------------------------------------------- timeline


def test_timeline_has_links_state_changes_evidence_and_review(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    deliver_checks(intake, run_id, head_sha="a1b2c3d4", conclusion="success")
    with store.transaction() as conn:
        store.update_run(
            conn, run_id, review_state="approved", structured_output='{"tests": "ok"}'
        )
    timeline = build_timeline(store, run_id)
    assert timeline is not None
    assert timeline["links"]["issue"].endswith("/issues/4242")
    assert (
        timeline["links"]["session"] == "https://app.devin.ai/sessions/devin-sim-0001"
    )
    assert timeline["links"]["pr"].endswith("/pull/91")
    states = [e["to"] for e in timeline["events"] if e["kind"] == "state"]
    assert states == [
        State.QUEUED.value,
        State.RUNNING.value,
        State.PR_OPEN.value,
        State.AWAITING_REVIEW.value,
    ]
    kinds = {e["kind"] for e in timeline["events"]}
    assert {"session", "pr", "checks", "review", "evidence"} <= kinds
    assert timeline["review"]["outcome"] == "approved"
    assert timeline["error"]["reason"] is None
    assert timeline["notifications"][0]["kind"] == "pr_opened"


def test_timeline_for_an_unknown_run_is_none(store: Store) -> None:
    assert build_timeline(store, "nope") is None


# ------------------------------------------------------------- HTTP


@pytest.fixture
def client(settings: Settings, store: Store) -> TestClient:
    return TestClient(create_app(settings, store))


def test_dashboard_endpoints(client: TestClient, intake: Intake, store: Store) -> None:
    run_id = queued_run(intake, store)
    deliver(intake, "issues", "issue_opened.json")

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Simulation" in page.text
    assert "Live" in page.text

    default = client.get("/api/dashboard")
    assert default.status_code == 200
    assert default.json()["env"] == "sim"

    live = client.get("/api/dashboard", params={"env": "live"})
    assert live.json()["tasks"] == []

    assert client.get("/api/dashboard", params={"env": "all"}).status_code == 400

    timeline = client.get(f"/api/dashboard/runs/{run_id}")
    assert timeline.status_code == 200
    assert timeline.json()["run_id"] == run_id
    assert client.get("/api/dashboard/runs/missing").status_code == 404
