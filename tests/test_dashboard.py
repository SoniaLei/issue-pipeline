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
"""What the dashboard says, and — more importantly — what it refuses to say."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dashboard import build_dashboard, build_timeline, is_verified
from app.devin_client import SimulatedDevinClient
from app.intake import derive_checks_state, Intake
from app.main import create_app
from app.slack_client import FakeSlackTransport
from app.states import State
from app.store import Store, utcnow
from app.worker import Worker
from tests.conftest import (
    deliver,
    load_fixture,
    load_script,
    next_delivery_id,
    REPO,
    substitute_run_id,
)


def send(intake: Intake, event: str, fixture: str, run_id: str, **over: Any) -> Any:
    payload = substitute_run_id(load_fixture(fixture), run_id)
    payload.update(over)
    return intake.handle(delivery_id=next_delivery_id(), event=event, payload=payload)


def run_to_pr(intake: Intake, worker: Worker, store: Store) -> str:
    """Approve, let the simulated session finish, deliver the PR webhook."""
    result = deliver(intake, "issues", "issue_labeled.json")
    run_id = str(result.run_id)
    for _ in range(10):
        worker.tick()
    send(intake, "pull_request", "pr_opened.json", run_id)
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.PR_OPEN.value
    return run_id


# ------------------------------------------------------------------ verification


def test_a_pr_is_opened_but_not_verified_until_checks_pass(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    board = build_dashboard(store, "sim")

    assert board["results"] == {
        "pr_opened": 1,
        "checks_passed": 0,
        "verified": 0,
        "merged": 0,
        "closed_unmerged": 0,
    }
    assert board["workload"]["pr_open"] == 1
    assert board["workload"]["awaiting_review"] == 0
    (task,) = board["tasks"]
    assert task["tests"]["checks"]["state"] == "unknown"
    assert task["review"]["state"] == "not_requested"
    assert board["review_gate"] == {
        "mode": "required",
        "states": {"not_requested": 1},
        "last_call_at": None,
    }
    # Devin said it added a test; that is reported, but it is not verification.
    assert task["tests"]["regression_test"] == "added"
    assert not task["verified"]
    assert store.get_run(run_id) is not None


def test_checks_passing_on_the_current_head_verifies(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_requested.json", run_id)
    assert build_dashboard(store, "sim")["tasks"][0]["tests"]["checks"]["state"] == (
        "pending"
    )

    send(intake, "check_suite", "check_suite_success.json", run_id)
    board = build_dashboard(store, "sim")

    # Checks alone are half the gate: reported as such, not as verified.
    run = store.get_run(run_id)
    assert run is not None
    assert not is_verified(run, None, "required")
    assert is_verified(run, None, "advisory")
    assert board["results"]["checks_passed"] == 1
    assert board["results"]["verified"] == 0
    assert board["workload"]["pr_open"] == 1
    (task,) = board["tasks"]
    assert task["checks_passed"]
    assert not task["verified"]
    assert task["review"]["state"] == "not_requested"
    assert "verified" not in [e["kind"] for e in store.events_for_run(run_id)]

    # Devin Review's verdict for the same commit arrives from GitHub.
    send(intake, "pull_request_review", "devin_review_clean.json", run_id)
    board = build_dashboard(store, "sim")

    run = store.get_run(run_id)
    assert run is not None
    review = store.review_for_head(run_id, run["head_sha"])
    assert review is not None
    assert review["findings"] == 0
    assert is_verified(run, review, "required")
    assert board["results"]["verified"] == 1
    (task,) = board["tasks"]
    assert task["review"]["state"] == "clear"
    assert task["review"]["head_sha"] == "a1b2c3d4"
    assert task["review"]["satisfied"]
    assert task["pr"]["review_state"] is None  # the bot is not a human reviewer
    assert board["workload"] == {
        "awaiting_approval": 0,
        "queued": 0,
        "running": 0,
        "pr_open": 0,
        "awaiting_review": 1,
        "blocked": 0,
        "failed": 0,
    }
    assert board["speed"]["review_ready"]["samples"] == 1
    assert board["speed"]["review_ready"]["median_seconds"] is not None
    assert [e["kind"] for e in store.events_for_run(run_id)][-1] == "verified"
    # The state machine is untouched: verification is a report-time fact.
    assert run["state"] == State.PR_OPEN.value
    # Verification is announced once, in the PR's thread; checks alone were not.
    assert [row["kind"] for row in store.all_notifications()] == [
        "pr_opened",
        "verified",
    ]


def test_a_failing_suite_fails_the_head_whatever_else_passed(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_success.json", run_id)
    payload = substitute_run_id(load_fixture("check_suite_failure.json"), run_id)
    payload["check_suite"]["id"] = 7002
    payload["check_suite"]["app"]["slug"] = "codecov"
    intake.handle(delivery_id=next_delivery_id(), event="check_suite", payload=payload)

    board = build_dashboard(store, "sim")
    assert board["results"]["verified"] == 0
    assert board["tasks"][0]["tests"]["checks"]["state"] == "failed"
    assert len(board["tasks"][0]["tests"]["checks"]["suites"]) == 2
    (item,) = board["attention"]
    assert item["reason"] == "checks_failed"
    assert "reply in the session" in item["next_action"]


def test_a_new_commit_resets_verification(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_success.json", run_id)
    send(intake, "pull_request_review", "devin_review_clean.json", run_id)
    assert build_dashboard(store, "sim")["results"]["verified"] == 1

    payload = substitute_run_id(load_fixture("pr_opened.json"), run_id)
    payload["action"] = "synchronize"
    payload["pull_request"]["head"]["sha"] = "e5f6a7b8"
    intake.handle(delivery_id=next_delivery_id(), event="pull_request", payload=payload)

    board = build_dashboard(store, "sim")
    assert board["results"]["verified"] == 0
    assert board["results"]["checks_passed"] == 0
    assert board["workload"]["pr_open"] == 1
    checks = board["tasks"][0]["tests"]["checks"]
    assert checks["state"] == "unknown"
    assert checks["head_sha"] == "e5f6a7b8"
    # The clear verdict belonged to a1b2c3d4; the new head starts unreviewed.
    review = board["tasks"][0]["review"]
    assert review["state"] == "not_requested"
    assert review["head_sha"] == "e5f6a7b8"
    assert review["findings"] is None
    kinds = [(e["kind"], e["reason"]) for e in store.events_for_run(run_id)]
    assert ("review_gate", "superseded") in kinds

    # A late suite for the *old* head must not re-verify the new one.
    stale = substitute_run_id(load_fixture("check_suite_success.json"), run_id)
    stale["check_suite"]["id"] = 7003
    result = intake.handle(
        delivery_id=next_delivery_id(), event="check_suite", payload=stale
    )
    assert not result.accepted
    assert build_dashboard(store, "sim")["results"]["verified"] == 0

    # Nor may a late Devin verdict for the old head clear the new one, even
    # once the new head's checks pass.
    new_suite = substitute_run_id(load_fixture("check_suite_success.json"), run_id)
    new_suite["check_suite"]["head_sha"] = "e5f6a7b8"
    intake.handle(
        delivery_id=next_delivery_id(), event="check_suite", payload=new_suite
    )
    late = substitute_run_id(load_fixture("devin_review_clean.json"), run_id)
    late["review"]["id"] = 5199
    late["pull_request"]["head"]["sha"] = "e5f6a7b8"
    late["review"]["commit_id"] = "a1b2c3d4"
    result = intake.handle(
        delivery_id=next_delivery_id(), event="pull_request_review", payload=late
    )
    assert result.accepted  # recorded against a1b2c3d4, as history
    board = build_dashboard(store, "sim")
    assert board["results"]["checks_passed"] == 1
    assert board["results"]["verified"] == 0
    assert board["tasks"][0]["review"]["state"] == "not_requested"


def test_a_check_suite_for_an_unknown_head_is_recorded_and_ignored(
    intake: Intake, store: Store
) -> None:
    result = send(intake, "check_suite", "check_suite_success.json", "nobody")
    assert not result.accepted
    assert result.reason == "check suite for an untracked head"
    assert build_dashboard(store, "sim")["results"]["verified"] == 0


def test_derive_checks_state_never_passes_on_silence(store: Store) -> None:
    assert derive_checks_state([]) == "unknown"


def test_suites_requested_before_the_pr_opens_are_adopted_and_keep_it_pending(
    intake: Intake, worker: Worker, store: Store
) -> None:
    """GitHub reports suites for the pushed commit before the PR carrying it
    exists. Two are requested; one finishes early. The run must not read the
    lone completed suite as a pass."""
    result = deliver(intake, "issues", "issue_labeled.json")
    run_id = str(result.run_id)
    for _ in range(10):
        worker.tick()

    requested = substitute_run_id(load_fixture("check_suite_requested.json"), run_id)
    second = substitute_run_id(load_fixture("check_suite_requested.json"), run_id)
    second["check_suite"]["id"] = 7002
    finished_first = substitute_run_id(load_fixture("check_suite_success.json"), run_id)
    for payload in (requested, second, finished_first):
        outcome = intake.handle(
            delivery_id=next_delivery_id(), event="check_suite", payload=payload
        )
        assert not outcome.accepted
        assert outcome.reason == "check suite for an untracked head"

    send(intake, "pull_request", "pr_opened.json", run_id)

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.PR_OPEN.value
    assert run["checks_state"] == "pending"
    assert not is_verified(run, None, "advisory")
    assert len(store.checks_for_head(run_id, "a1b2c3d4")) == 2
    assert build_dashboard(store, "sim")["results"]["checks_passed"] == 0

    # Only once the second suite completes do the checks pass; with the
    # review verdict already on GitHub, that is the moment the head verifies.
    send(intake, "pull_request_review", "devin_review_clean.json", run_id)
    kinds = [row["kind"] for row in store.events_for_run(run_id)]
    assert kinds.count("verified") == 0
    finished_second = substitute_run_id(
        load_fixture("check_suite_success.json"), run_id
    )
    finished_second["check_suite"]["id"] = 7002
    intake.handle(
        delivery_id=next_delivery_id(), event="check_suite", payload=finished_second
    )
    run = store.get_run(run_id)
    assert run is not None
    assert is_verified(run, store.review_for_head(run_id, "a1b2c3d4"), "required")
    kinds = [row["kind"] for row in store.events_for_run(run_id)]
    assert kinds.count("verified") == 1


# ---------------------------------------------------------------- merged / review


def test_merged_is_github_saying_so(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_success.json", run_id)
    send(intake, "pull_request_review", "devin_review_clean.json", run_id)
    send(intake, "pull_request_review", "pr_review_approved.json", run_id)
    send(intake, "pull_request", "pr_merged.json", run_id)

    board = build_dashboard(store, "sim")
    assert board["results"]["merged"] == 1
    assert board["results"]["verified"] == 1
    assert board["tasks"][0]["bucket"] == "merged"
    assert board["tasks"][0]["timing"]["ended_at"] is not None
    today = utcnow().date().isoformat()
    assert board["throughput"][-1] == {"day": today, "verified": 1, "merged": 1}

    timeline = build_timeline(store, board["tasks"][0]["task_id"])
    assert timeline is not None
    assert timeline["review"]["outcome"] == "merged"
    assert timeline["review"]["review_state"] == "approved"
    assert timeline["review"]["devin_review"]["state"] == "clear"
    (history,) = timeline["review"]["devin_review_history"]
    assert history["head_sha"] == "a1b2c3d4"
    assert history["current"]
    kinds = [e["kind"] for e in timeline["events"]]
    for kind in (
        "state",
        "session",
        "pr",
        "checks",
        "review_gate",
        "verified",
        "review",
    ):
        assert kind in kinds
    states = [e["to_state"] for e in timeline["events"] if e["kind"] == "state"]
    assert states[0] == State.QUEUED.value
    assert states[-1] == State.MERGED.value
    reviews = [e for e in timeline["events"] if e["kind"] == "review"]
    assert [e["detail"]["reviewer"] for e in reviews] == ["SoniaLei"]
    assert timeline["runs"][0]["session"]["url"] is not None
    assert timeline["runs"][0]["pr"]["url"] is not None
    assert timeline["notifications"][0]["kind"] == "pr_opened"


# --------------------------------------------------------------- blocked / failed


def test_blocked_runs_show_reason_and_next_action(
    intake: Intake, store: Store, settings: Settings, slack: FakeSlackTransport
) -> None:
    devin = SimulatedDevinClient(script=load_script("simulated_session_blocked.json"))
    worker = Worker(store, settings, devin, slack, owner="test-worker")
    deliver(intake, "issues", "issue_labeled.json")
    for _ in range(10):
        worker.tick()

    board = build_dashboard(store, "sim")
    assert board["workload"]["blocked"] == 1
    (item,) = board["attention"]
    assert item["blocker"]
    assert item["next_action"]
    assert item["age_seconds"] is not None
    assert board["tasks"][0]["failure_reason"] == item["reason"]


def test_a_queued_run_held_by_a_limit_is_flagged(
    intake: Intake, worker: Worker, store: Store
) -> None:
    result = deliver(intake, "issues", "issue_labeled.json")
    with store.transaction() as conn:
        store.update_run(conn, str(result.run_id), failure_reason="concurrency")

    (item,) = build_dashboard(store, "sim")["attention"]
    assert item["reason"] == "concurrency"
    assert "queue" in item["blocker"]


# ---------------------------------------------------------- environment isolation


def test_live_and_simulated_runs_are_never_summed(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_success.json", run_id)
    send(intake, "pull_request", "pr_merged.json", run_id)

    live = build_dashboard(store, "live")
    sim = build_dashboard(store, "sim")
    assert sim["results"]["merged"] == 1
    assert live["results"] == {
        "pr_opened": 0,
        "checks_passed": 0,
        "verified": 0,
        "merged": 0,
        "closed_unmerged": 0,
    }
    assert live["tasks"] == []
    assert live["speed"]["review_ready"]["samples"] == 0
    assert all(d["verified"] == d["merged"] == 0 for d in live["throughput"])
    assert live["envs_available"] == sim["envs_available"] == {"sim": 1}


# --------------------------------------------------------------- health / fresh


def test_health_reports_polls_heartbeat_and_slack_failures(
    intake: Intake, worker: Worker, store: Store, slack: FakeSlackTransport
) -> None:
    run_id = run_to_pr(intake, worker, store)
    slack.fail_times = 1
    send(intake, "pull_request", "pr_merged.json", run_id)
    worker.drain_outbox()

    board = build_dashboard(store, "sim", worker_stale_after_seconds=3600)
    health = board["health"]
    assert health["github"]["last_delivery_at"] is not None
    assert health["devin"]["last_poll_at"] is not None
    assert health["worker"]["stale"] is False
    assert health["worker"]["owner"] == "test-worker"
    assert board["data_as_of"] is not None
    assert board["generated_at"] >= board["data_as_of"][:19]
    assert health["slack"]["failed"] + health["slack"]["pending"] >= 1
    assert board["tasks"][0]["slack"]["status"] in {"failed", "pending"}
    assert board["tasks"][0]["slack"]["last_error"]
    assert health["slack"]["last_error"] == board["tasks"][0]["slack"]["last_error"]


def test_serving_the_dashboard_does_not_refresh_data_as_of(
    client: TestClient, store: Store
) -> None:
    store.heartbeat("worker", "w1")
    first = client.get("/api/dashboard").json()["data_as_of"]
    time.sleep(0.01)
    second = client.get("/api/dashboard").json()["data_as_of"]
    assert first == second
    assert {row["component"] for row in store.heartbeats()} == {"api", "worker"}


def test_a_silent_worker_is_reported_stale(store: Store) -> None:
    board = build_dashboard(store, "sim")
    assert board["health"]["worker"]["stale"] is True
    assert board["health"]["worker"]["last_tick_at"] is None

    store.heartbeat("worker", "w1")
    future = utcnow() + timedelta(seconds=10)
    assert not build_dashboard(store, "sim", now=future)["health"]["worker"]["stale"]
    much_later = utcnow() + timedelta(hours=2)
    assert build_dashboard(store, "sim", now=much_later)["health"]["worker"]["stale"]


# ------------------------------------------------------------------------ HTTP


@pytest.fixture
def client(settings: Settings, store: Store) -> TestClient:
    return TestClient(create_app(settings, store))


def test_dashboard_endpoints(
    client: TestClient, intake: Intake, worker: Worker, store: Store
) -> None:
    run_to_pr(intake, worker, store)

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Simulation" in page.text
    assert "Live" in page.text
    assert 'const DEFAULT_ENV = "sim";' in page.text
    # The per-state summary counts PRs; that count must not be drawn as a
    # finding count on the pill.
    assert "findings: null, findings_by_kind: {} }), ` ×${n} `" in page.text

    default = client.get("/api/dashboard").json()
    assert default["env"] == "sim"
    assert default["results"]["pr_opened"] == 1
    assert client.get("/api/dashboard?env=live").json()["results"]["pr_opened"] == 0
    assert client.get("/api/dashboard?env=all").status_code == 400

    task_id = default["tasks"][0]["task_id"]
    timeline = client.get(f"/api/tasks/{task_id}/timeline").json()
    assert timeline["issue"]["number"] == 4242
    assert timeline["runs"][0]["pr"]["number"] == 91
    assert client.get("/api/tasks/999/timeline").status_code == 404
    # Serving the API leaves an API heartbeat behind.
    assert {row["component"] for row in store.heartbeats()} >= {"api", "worker"}


def test_github_health_is_labelled_as_spanning_environments(store: Store) -> None:
    # Deliveries carry no env, so the figure cannot be scoped like the others.
    board = build_dashboard(store, "live", worker_stale_after_seconds=3600)
    assert board["health"]["github"]["scope"] == "all environments"


OTHER_REPO = "SoniaLei/another-service"


def test_repo_filter_narrows_every_run_figure(
    settings: Settings,
    store: Store,
    intake: Intake,
    worker: Worker,
    client: TestClient,
) -> None:
    run_to_pr(intake, worker, store)
    other = Intake(
        store,
        replace(
            settings, repo_allowlist=settings.repo_allowlist | {OTHER_REPO.lower()}
        ),
    )
    payload = load_fixture("issue_labeled.json")
    payload["repository"] = {
        "full_name": OTHER_REPO,
        "name": "another-service",
        "owner": {"login": "SoniaLei"},
    }
    assert other.handle(
        delivery_id=next_delivery_id(), event="issues", payload=payload
    ).run_id

    everything = build_dashboard(store, "sim")
    assert everything["repo"] is None
    assert everything["repos_available"] == [
        {"repo": OTHER_REPO, "runs": 1},
        {"repo": REPO, "runs": 1},
    ]
    assert everything["totals"]["tasks"] == 2

    demo = build_dashboard(store, "sim", repo=REPO.upper())
    assert demo["repo"] == REPO
    assert [t["repo"] for t in demo["tasks"]] == [REPO]
    assert demo["results"]["pr_opened"] == 1

    second = build_dashboard(store, "sim", repo=OTHER_REPO)
    assert [t["repo"] for t in second["tasks"]] == [OTHER_REPO]
    assert second["results"]["pr_opened"] == 0
    assert second["repos_available"] == everything["repos_available"]

    # Devin counts per service user, so the cross-check is never narrowed.
    for board in (demo, second):
        analytics = board["health"]["devin_analytics"]
        assert analytics["scope"] == "all repositories"
        assert (
            analytics["pipeline"] == everything["health"]["devin_analytics"]["pipeline"]
        )

    unknown = build_dashboard(store, "sim", repo="someone/else")
    assert unknown["repo"] == "someone/else"
    assert unknown["tasks"] == []

    api = client.get("/api/dashboard", params={"repo": OTHER_REPO}).json()
    assert [t["repo"] for t in api["tasks"]] == [OTHER_REPO]
    assert len(client.get("/api/dashboard").json()["tasks"]) == 2


def test_repo_picker_folds_names_that_differ_only_in_case(
    settings: Settings, store: Store, intake: Intake
) -> None:
    deliver(intake, "issues", "issue_labeled.json")
    payload = load_fixture("issue_labeled.json")
    payload["issue"] = {**payload["issue"], "number": 4243}
    payload["repository"] = {**payload["repository"], "full_name": REPO.upper()}
    intake.handle(delivery_id=next_delivery_id(), event="issues", payload=payload)

    board = build_dashboard(store, "sim", repo=REPO.lower())
    [entry] = board["repos_available"]
    assert entry["runs"] == 2
    assert entry["repo"].lower() == REPO.lower()
    assert board["repo"] == entry["repo"]
    assert board["totals"]["tasks"] == 2


def test_throughput_window_is_adjustable(
    client: TestClient, intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "pull_request", "pr_merged.json", run_id)
    today = utcnow().date().isoformat()
    later = utcnow() + timedelta(days=20)

    default = build_dashboard(store, "sim", now=later)
    assert default["throughput_days"] == 14
    assert len(default["throughput"]) == 14
    assert all(d["merged"] == 0 for d in default["throughput"])

    wide = build_dashboard(store, "sim", now=later, days=30)
    assert wide["throughput_days"] == 30
    assert len(wide["throughput"]) == 30
    assert wide["throughput"][-21] == {"day": today, "verified": 0, "merged": 1}
    with pytest.raises(ValueError, match="days"):
        build_dashboard(store, "sim", days=91)

    api = client.get("/api/dashboard?env=sim&days=7").json()
    assert api["throughput_days"] == 7
    assert len(api["throughput"]) == 7
    assert api["throughput"][-1]["merged"] == 1
    assert len(client.get("/api/dashboard?env=sim").json()["throughput"]) == 14
    for bad in ("0", "91", "abc", "-3", "\u00b2", "9" * 5000):
        assert client.get(f"/api/dashboard?days={bad}").status_code == 400
