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
"""Devin analytics: the client contract, the worker's read path, and what the
dashboard makes of it.

Everything here is provider testimony laid beside GitHub's facts. The tests
pin three rules: a missing figure is never shown as zero, an analytics failure
never touches a run, and Devin's counts are compared with the pipeline's, not
written over them.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.dashboard import build_dashboard, build_timeline
from app.devin_client import (
    DailyConsumption,
    DevinError,
    LiveDevinClient,
    SessionInsights,
    SimulatedDevinClient,
)
from app.intake import Intake
from app.slack_client import FakeSlackTransport
from app.states import State
from app.store import Store
from app.worker import Worker
from tests.conftest import deliver, load_script
from tests.test_dashboard import run_to_pr, send

ORG = "org-test"
BASE = f"https://api.example/v3/organizations/{ORG}"


def _client(handler: httpx.MockTransport) -> LiveDevinClient:
    return LiveDevinClient(
        base_url="https://api.example",
        org_id=ORG,
        token="cog_test",
        transport=handler,
    )


# ------------------------------------------------------------------ live client


def test_insights_are_read_from_the_session_insights_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/sessions/devin-1/insights"
        return httpx.Response(
            200,
            json={
                "session_id": "devin-1",
                "status": "exit",
                "status_detail": "finished",
                "acus_consumed": 3.5,
                "session_size": "m",
                "category": "bug_fixing",
                "service_user_id": "service-user-1",
                "num_user_messages": 1,
                "num_devin_messages": "4",
                "analysis_status": "completed",
                "analysis": {
                    "issues": [],
                    "action_items": [
                        {"type": "repo_config", "action_item": "pre-build"}
                    ],
                },
            },
        )

    insights = _client(httpx.MockTransport(handler)).get_session_insights("devin-1")

    assert insights.acus_consumed == 3.5
    assert insights.session_size == "m"
    assert insights.service_user_id == "service-user-1"
    assert insights.num_devin_messages == 4
    assert insights.analysis_status == "completed"
    assert insights.analysis is not None
    assert insights.analysis["action_items"][0]["type"] == "repo_config"


def test_a_partial_insights_response_is_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"devin_id": "devin-1", "status": "running"})

    insights = _client(httpx.MockTransport(handler)).get_session_insights("devin-1")

    assert insights.session_id == "devin-1"
    assert insights.acus_consumed is None
    assert insights.analysis_status is None
    assert insights.analysis is None


def test_consumption_uses_the_billing_endpoint_and_epoch_days() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/consumption/daily/sessions/devin-1"
        return httpx.Response(
            200,
            json={
                "total_acus": 4.25,
                "consumption_by_date": [
                    {"date": 1790323200, "acus": 4.25, "acus_by_product": {}}
                ],
            },
        )

    consumption = _client(httpx.MockTransport(handler)).get_session_consumption(
        "devin-1"
    )

    assert consumption.total_acus == 4.25
    assert consumption.by_date == (("2026-09-25T08:00:00+00:00", 4.25),)


def test_consumption_not_yet_published_is_zero_rows_not_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_acus": 0, "consumption_by_date": []})

    consumption = _client(httpx.MockTransport(handler)).get_session_consumption(
        "devin-1"
    )
    assert consumption == DailyConsumption(total_acus=0.0)


def test_insights_generate_is_a_post_that_returns_the_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == f"{BASE}/sessions/devin-1/insights/generate"
        return httpx.Response(
            200, json={"session_id": "devin-1", "status": "already_exists"}
        )

    assert (
        _client(httpx.MockTransport(handler)).request_session_insights("devin-1")
        == "already_exists"
    )


def test_provider_metrics_are_scoped_to_service_users_and_a_window() -> None:
    after = datetime(2026, 9, 1, tzinfo=timezone.utc)
    before = datetime(2026, 9, 28, tzinfo=timezone.utc)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.url.params.get_list("service_user_ids") == ["service-user-1"]
        assert request.url.params["time_after"] == str(int(after.timestamp()))
        assert request.url.params["time_before"] == str(int(before.timestamp()))
        if request.url.path.endswith("/metrics/prs"):
            return httpx.Response(
                200,
                json={
                    "prs_created_count": 3,
                    "prs_opened_count": 1,
                    "prs_merged_count": 2,
                    "prs_closed_count": 0,
                },
            )
        return httpx.Response(
            200,
            json={
                "sessions_created_count": 4,
                "sessions_with_merged_prs_count": 2,
                "avg_acus_per_session": 5.5,
                "sessions_created_by_size": {"s": 3, "l": 1},
            },
        )

    metrics = _client(httpx.MockTransport(handler)).get_provider_metrics(
        service_user_ids=["service-user-1"], time_after=after, time_before=before
    )

    assert seen == [
        f"/v3/organizations/{ORG}/metrics/prs",
        f"/v3/organizations/{ORG}/metrics/sessions",
    ]
    assert (metrics.prs_created, metrics.prs_merged) == (3, 2)
    assert metrics.sessions_created == 4
    assert metrics.avg_acus_per_session == 5.5
    assert metrics.sessions_by_size == {"s": 3, "l": 1}


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(401, False), (403, False), (404, False), (429, True), (500, True), (503, True)],
)
def test_analytics_http_failures_keep_their_retry_class(
    status: int, retryable: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"status": status})

    with pytest.raises(DevinError) as excinfo:
        _client(httpx.MockTransport(handler)).get_session_consumption("devin-1")
    assert excinfo.value.retryable is retryable


def test_an_analytics_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DevinError) as excinfo:
        _client(httpx.MockTransport(handler)).get_session_insights("devin-1")
    assert excinfo.value.retryable is True


# --------------------------------------------------------------- worker + store


def _insights_row(store: Store, run_id: str) -> Any:
    row = store.insights_for_run(run_id)
    assert row is not None
    return row


def test_the_worker_stores_insights_beside_the_run_without_touching_it(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    before = store.get_run(run_id)
    assert before is not None

    row = _insights_row(store, run_id)
    assert row["session_id"] == "devin-sim-0001"
    assert row["env"] == "sim"
    assert row["service_user_id"] == "service-user-sim"
    assert row["billed_acus"] == pytest.approx(4.25)
    assert row["analysis_status"] == "completed"
    assert json.loads(row["analysis"])["action_items"][1]["type"] == "knowledge"
    assert row["last_error"] is None

    after = store.get_run(run_id)
    assert after is not None
    assert dict(after) == dict(before)
    assert after["state"] == State.PR_OPEN.value

    events = store.events_for_task(int(after["task_id"]))
    analysed = [e for e in events if e["kind"] == "insights"]
    assert len(analysed) == 1
    assert json.loads(analysed[0]["detail"])["action_items"] == 2


def test_unchanged_runs_are_not_re_read_before_the_interval(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    row = _insights_row(store, run_id)
    count = row["fetch_count"]

    for _ in range(3):
        worker.refresh_analytics()

    assert _insights_row(store, run_id)["fetch_count"] == count
    events = store.events_for_task(int(_run(store, run_id)["task_id"]))
    assert len([e for e in events if e["kind"] == "insights"]) == 1


def test_a_state_change_triggers_a_re_read_that_keeps_one_row(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    count = _insights_row(store, run_id)["fetch_count"]

    send(intake, "check_suite", "check_suite_success.json", run_id)
    send(intake, "pull_request", "pr_merged.json", run_id)
    worker.refresh_analytics()

    row = _insights_row(store, run_id)
    assert row["fetch_count"] == count + 1
    assert row["run_state"] == State.MERGED.value
    assert len(store.insights_for_env("sim")) == 1


def test_an_analytics_failure_is_recorded_and_changes_nothing_else(
    intake: Intake, store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    class Broken(SimulatedDevinClient):
        def get_session_insights(self, session_id: str) -> SessionInsights:
            raise DevinError("insights returned 503", retryable=True)

    devin = Broken(script=load_script("simulated_session_events.json"))
    worker = Worker(store, settings, devin, slack, owner="test")
    run_id = str(deliver(intake, "issues", "issue_labeled.json").run_id)

    for _ in range(6):
        assert worker.tick() in {True, False}

    run = _run(store, run_id)
    assert run["state"] == State.PR_OPEN.value
    assert run["acus_consumed"] == pytest.approx(4.25)
    row = _insights_row(store, run_id)
    assert row["last_error"] == "insights returned 503"
    assert row["acus_consumed"] is None
    assert row["billed_acus"] is None
    # No service user has been observed, so the org counts are never asked for.
    assert store.provider_metrics("sim") is None

    board = build_dashboard(store, "sim")
    insights = board["tasks"][0]["insights"]
    assert insights["available"] is True
    assert insights["last_error"] == "insights returned 503"
    # The poll figure is still shown, but named as a poll figure.
    assert insights["cost"] == {
        "acus": 4.25,
        "source": "poll",
        "billed_acus": None,
        "by_day": [],
    }
    assert board["health"]["devin_analytics"]["status"] == "unavailable"


def test_a_failed_metrics_refresh_keeps_the_last_good_numbers(
    intake: Intake, worker: Worker, store: Store, devin: SimulatedDevinClient
) -> None:
    run_to_pr(intake, worker, store)
    good = store.provider_metrics("sim")
    assert good is not None
    assert good["metrics"] is not None

    def fail(**kwargs: Any) -> Any:
        raise DevinError("metrics returned 429", retryable=True)

    devin.get_provider_metrics = fail  # type: ignore[method-assign]
    worker._refresh_metrics("sim", datetime.now(timezone.utc))

    row = store.provider_metrics("sim")
    assert row is not None
    assert row["metrics"] == good["metrics"]
    assert row["last_error"] == "metrics returned 429"
    assert build_dashboard(store, "sim")["health"]["devin_analytics"]["last_error"] == (
        "metrics returned 429"
    )


def test_analytics_can_be_switched_off(
    intake: Intake, store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    quiet = dataclasses.replace(settings, analytics_enabled=False)
    devin = SimulatedDevinClient(script=load_script("simulated_session_events.json"))
    worker = Worker(store, quiet, devin, slack, owner="test")
    run_id = str(deliver(intake, "issues", "issue_labeled.json").run_id)

    for _ in range(6):
        worker.tick()

    assert _run(store, run_id)["state"] == State.PR_OPEN.value
    assert store.insights_for_run(run_id) is None
    assert store.provider_metrics("sim") is None


# ------------------------------------------------------------------- dashboard


def test_cost_is_computed_from_billing_grade_figures_when_published(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_success.json", run_id)
    send(intake, "pull_request", "pr_merged.json", run_id)

    cost = build_dashboard(store, "sim")["cost"]
    assert cost["acus_total"] == pytest.approx(4.25)
    assert cost["acus_per_pr_opened"] == pytest.approx(4.25)
    assert cost["acus_per_verified_pr"] == pytest.approx(4.25)
    assert cost["acus_per_merged_pr"] == pytest.approx(4.25)
    assert cost["acus_median_per_run"] == pytest.approx(4.25)
    assert cost["acus_without_pr"] == 0
    assert cost["denominators"] == {"pr_opened": 1, "verified": 1, "merged": 1}
    assert cost["sources"] == {"billing": 1}
    assert cost["coverage"]["priced"] == 1
    assert cost["coverage"]["no_cost_yet"] == 0
    assert cost["coverage"]["analysed"] == 1
    assert cost["session_sizes"]["s"] == 1
    assert cost["large_share"] == 0
    assert cost["categories"] == {"bug_fixing": 1}
    assert cost["action_items"]["repo_config"] == 1
    assert cost["action_items"]["knowledge"] == 1
    assert cost["skills"] == {"good": 1, "bad": 0}
    assert cost["top_action_items"][0]["issue_number"] == 4242


def test_missing_consumption_falls_back_to_the_session_total_and_says_so(
    intake: Intake, store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    class Unbilled(SimulatedDevinClient):
        def get_session_consumption(self, session_id: str) -> DailyConsumption:
            return DailyConsumption(total_acus=0.0)

    devin = Unbilled(script=load_script("simulated_session_events.json"))
    worker = Worker(store, settings, devin, slack, owner="test")
    run_id = run_to_pr(intake, worker, store)

    board = build_dashboard(store, "sim")
    cost = board["tasks"][0]["insights"]["cost"]
    assert cost["acus"] == pytest.approx(4.25)
    assert cost["source"] == "session"
    assert cost["billed_acus"] == 0.0
    assert board["cost"]["sources"] == {"session": 1}
    assert board["cost"]["coverage"]["billing_grade"] == 0
    assert _insights_row(store, run_id)["consumption"] == "[]"


def test_a_session_with_no_figure_yet_is_pending_not_free(
    intake: Intake, store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    script = load_script("simulated_session_events.json")
    for step in script:
        step.pop("acus_consumed", None)
    devin = SimulatedDevinClient(script=script)
    worker = Worker(store, settings, devin, slack, owner="test")
    run_id = run_to_pr(intake, worker, store)

    board = build_dashboard(store, "sim")
    insights = board["tasks"][0]["insights"]
    assert insights["available"] is True
    assert insights["cost"]["acus"] is None
    assert insights["cost"]["source"] is None
    cost = board["cost"]
    assert cost["acus_total"] == 0
    assert cost["acus_per_pr_opened"] is None
    assert cost["acus_median_per_run"] is None
    assert cost["coverage"] == {
        "runs_with_session": 1,
        "priced": 0,
        "no_cost_yet": 1,
        "billing_grade": 0,
        "analysed": 1,
        "analysis_pending": 0,
    }
    assert store.get_run(run_id) is not None


def test_drift_compares_devin_with_github_and_never_reconciles(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_success.json", run_id)
    send(intake, "pull_request", "pr_merged.json", run_id)
    worker.refresh_analytics()

    board = build_dashboard(store, "sim")
    # GitHub said merged; the simulated provider still shows the PR open.
    assert board["results"]["merged"] == 1
    check = board["health"]["devin_analytics"]
    assert check["status"] == "drift"
    assert check["service_user_ids"] == ["service-user-sim"]
    assert check["pipeline"] == {
        "sessions_created": 1,
        "prs_opened": 1,
        "prs_merged": 1,
    }
    assert check["provider"]["sessions_created"] == 1
    assert check["provider"]["prs_created"] == 1
    assert check["provider"]["prs_merged"] == 0
    assert check["drift"] == {"sessions": 0, "prs": 0, "merged": -1}
    assert check["window"]["after"] < check["window"]["before"]


def test_agreeing_counts_report_ok(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_to_pr(intake, worker, store)
    check = build_dashboard(store, "sim")["health"]["devin_analytics"]
    assert check["status"] == "ok"
    assert check["drift"] == {"sessions": 0, "prs": 0, "merged": 0}


def test_provider_analytics_stay_inside_their_environment(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_to_pr(intake, worker, store)

    live = build_dashboard(store, "live")
    assert live["cost"]["acus_total"] == 0
    assert live["cost"]["coverage"]["runs_with_session"] == 0
    assert live["cost"]["sources"] == {}
    assert live["health"]["devin_analytics"]["status"] == "unavailable"
    assert live["health"]["devin_analytics"]["provider"] is None
    assert store.service_user_ids("live") == []
    assert build_dashboard(store, "sim")["cost"]["acus_total"] == pytest.approx(4.25)


def test_the_timeline_carries_devins_account_of_the_run(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_to_pr(intake, worker, store)
    board = build_dashboard(store, "sim")
    timeline = build_timeline(store, board["tasks"][0]["task_id"])
    assert timeline is not None

    insights = timeline["runs"][0]["insights"]
    assert insights["available"] is True
    assert insights["cost"]["source"] == "billing"
    assert len(insights["cost"]["by_day"]) == 1
    assert insights["session_size"] == "s"
    assert insights["category"] == "bug_fixing"
    assert insights["analysis_status"] == "completed"
    analysis = insights["analysis"]
    assert analysis["issues"][0]["label"] == "machine_setup"
    assert [a["type"] for a in analysis["action_items"]] == [
        "repo_config",
        "knowledge",
    ]
    assert analysis["skills"]["good"][0]["name"] == "superset-local-runtime"
    assert analysis["suggested_prompt"] is None
    assert [e["kind"] for e in timeline["events"]].count("insights") == 1


def _run(store: Store, run_id: str) -> Any:
    run = store.get_run(run_id)
    assert run is not None
    return run


def test_settled_rows_stop_being_candidates(store: Store, settings: Settings) -> None:
    now = datetime.now(timezone.utc)
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO tasks (id, repo, issue_number, issue_title, issue_state,"
            " created_at, updated_at) VALUES (1, 'o/r', 1, 't', 'open', ?, ?)",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            "INSERT INTO runs (id, task_id, env, state, session_id, created_at,"
            " updated_at) VALUES ('r1', 1, 'sim', 'merged', 'devin-1', ?, ?)",
            (now.isoformat(), now.isoformat()),
        )
        store.upsert_session_insights(
            conn,
            "r1",
            session_id="devin-1",
            env="sim",
            run_state="merged",
            session_status=None,
            session_status_detail=None,
            settled=1,
        )
    stale = now + timedelta(days=2)
    assert store.insights_candidates("sim", stale) == []
