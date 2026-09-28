# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
# either express or implied.  See the License for the specific
# language governing permissions and limitations under the License.
"""The Devin Review gate (D-033).

Two sources, kept apart: the pr-reviews API says whether a review of a
commit was requested and how far it got; GitHub's review by the Devin bot
says what it found. Only the second, for the run's exact current head, can
clear the gate. Everything else here is about not confusing the two, not
asking twice, not dying on provider errors, and not letting an old commit's
verdict speak for a new one.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.dashboard import build_dashboard, build_timeline
from app.devin_client import (
    DevinError,
    LiveDevinClient,
    PullRequestReview,
    SimulatedDevinClient,
)
from app.intake import Intake
from app.review_gate import finding_kind, gate_state, parse_summary
from app.slack_client import FakeSlackTransport
from app.store import Store
from app.worker import Worker
from tests.conftest import (
    load_fixture,
    load_script,
    next_delivery_id,
    substitute_run_id,
)
from tests.test_dashboard import run_to_pr, send

PR_URL = "https://github.com/SoniaLei/superset-cognition-demo/pull/91"
HEAD = "a1b2c3d4"


def _events(store: Store, run_id: str, kind: str = "review_gate") -> list[str]:
    return [str(e["reason"]) for e in store.events_for_run(run_id) if e["kind"] == kind]


@pytest.fixture
def quick(settings: Settings) -> Settings:
    """Poll immediately so a test can walk the review through its statuses."""
    return dataclasses.replace(settings, review_poll_seconds=0, review_max_attempts=3)


@pytest.fixture
def worker(
    store: Store,
    quick: Settings,
    devin: SimulatedDevinClient,
    slack: FakeSlackTransport,
) -> Worker:
    return Worker(store, quick, devin, slack, owner="test-worker")


@pytest.fixture
def intake(store: Store, quick: Settings) -> Intake:
    return Intake(store, quick)


# ------------------------------------------------------------- parsing the bot


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("**Devin Review** found 5 potential issues.", 5),
        ("**Devin Review** found 1 potential issue.", 1),
        ("**Devin Review**: No Issues Found", 0),
        ("Devin Review found 0 potential issues", 0),
        ("Looks good to me", None),
        (None, None),
    ],
)
def test_the_summary_comment_yields_a_finding_count(
    body: str | None, expected: int | None
) -> None:
    assert parse_summary(body) == expected


def test_inline_markers_name_their_kind_or_fall_back_to_the_id_prefix() -> None:
    explicit = '<!-- devin-review-comment {"id": "BUG_x", "kind": "security"} -->'
    assert finding_kind(explicit) == "security"
    from_id = '<!-- devin-review-comment {"id": "BUG_pr-review-job-1_0"} -->'
    assert finding_kind(from_id) == "bug"
    assert finding_kind('<!-- devin-review-comment {"id": "weird"} -->') == "other"
    assert finding_kind("<!-- devin-review-comment not-json -->") is None
    assert finding_kind("a human comment") is None


def test_gate_state_words() -> None:
    assert gate_state(None, "off") == "off"
    assert gate_state(None, "required") == "not_requested"


# ------------------------------------------------------- the worker's requests


def test_an_open_pr_gets_one_review_request_for_its_head(
    intake: Intake, worker: Worker, store: Store, devin: SimulatedDevinClient
) -> None:
    run_id = run_to_pr(intake, worker, store)
    assert devin.review_requests == []

    worker.tick()  # idle tick: the gate runs
    assert devin.review_requests == [(PR_URL, HEAD)]
    row = store.review_for_head(run_id, HEAD)
    assert row is not None
    assert row["status"] == "pending"
    assert row["requested_at"] is not None
    assert row["findings"] is None

    # Further idle ticks read, never ask again: pending → running → completed.
    for _ in range(3):
        worker.tick()
    assert devin.review_requests == [(PR_URL, HEAD)]
    row = store.review_for_head(run_id, HEAD)
    assert row is not None
    assert row["status"] == "completed"
    assert _events(store, run_id) == ["pending", "running", "completed"]

    # A completed API status is not a verdict. The head is still not verified
    # and the gate waits for the bot's GitHub review.
    send(intake, "check_suite", "check_suite_success.json", run_id)
    board = build_dashboard(store, "sim")
    (task,) = board["tasks"]
    assert task["checks_passed"]
    assert not task["verified"]
    assert task["review"]["state"] == "awaiting_verdict"
    assert task["review"]["status"] == "completed"
    assert board["review_gate"]["states"] == {"awaiting_verdict": 1}
    assert board["review_gate"]["last_call_at"] is not None

    # Terminal: the worker stops reading this head.
    assert not worker.refresh_review_gate()

    send(intake, "pull_request_review", "devin_review_clean.json", run_id)
    (task,) = build_dashboard(store, "sim")["tasks"]
    assert task["verified"]
    assert task["review"]["state"] == "clear"
    assert task["review"]["review_url"] is not None


def test_a_verdict_that_arrived_first_means_no_request_is_made(
    intake: Intake, worker: Worker, store: Store, devin: SimulatedDevinClient
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "pull_request_review", "devin_review_findings.json", run_id)

    assert not worker.refresh_review_gate()
    assert devin.review_requests == []
    row = store.review_for_head(run_id, HEAD)
    assert row is not None
    assert row["findings"] == 2
    assert row["status"] is None


def test_the_poll_interval_is_respected(
    intake: Intake, store: Store, settings: Settings, devin: SimulatedDevinClient
) -> None:
    slow = dataclasses.replace(settings, review_poll_seconds=3600)
    worker = Worker(store, slow, devin, FakeSlackTransport(), owner="w")
    run_id = run_to_pr(Intake(store, slow), worker, store)

    assert worker.refresh_review_gate()
    assert not worker.refresh_review_gate()
    assert devin.review_requests == [(PR_URL, HEAD)]
    row = store.review_for_head(run_id, HEAD)
    assert row is not None
    assert row["status"] == "pending"


def test_a_new_head_starts_its_own_review_and_keeps_the_old_row(
    intake: Intake, worker: Worker, store: Store, devin: SimulatedDevinClient
) -> None:
    run_id = run_to_pr(intake, worker, store)
    for _ in range(4):
        worker.tick()
    assert _events(store, run_id)[-1] == "completed"

    payload = substitute_run_id(load_fixture("pr_opened.json"), run_id)
    payload["action"] = "synchronize"
    payload["pull_request"]["head"]["sha"] = "e5f6a7b8"
    intake.handle(delivery_id=next_delivery_id(), event="pull_request", payload=payload)
    devin.pr_heads[PR_URL] = "e5f6a7b8"

    assert worker.refresh_review_gate()
    assert devin.review_requests == [(PR_URL, HEAD), (PR_URL, "e5f6a7b8")]
    rows = store.reviews_for_run(run_id)
    assert {r["head_sha"]: r["status"] for r in rows} == {
        HEAD: "completed",
        "e5f6a7b8": "pending",
    }
    timeline = build_timeline(store, 1, review_gate_mode="required")
    assert timeline is not None
    history = timeline["review"]["devin_review_history"]
    assert [(h["head_sha"], h["current"]) for h in history] == [
        (HEAD, False),
        ("e5f6a7b8", True),
    ]


def test_devin_reviewing_a_newer_commit_is_not_a_review_of_ours(
    intake: Intake, worker: Worker, store: Store, devin: SimulatedDevinClient
) -> None:
    """GitHub moved on before the worker asked: the request reviews the new
    head, and the head the run still knows is marked skipped, not pending."""
    run_id = run_to_pr(intake, worker, store)
    devin.pr_heads[PR_URL] = "e5f6a7b8"

    worker.tick()
    ours = store.review_for_head(run_id, HEAD)
    assert ours is not None
    assert ours["status"] == "skipped"
    assert "newer commit e5f6a7b8" in str(ours["last_error"])
    theirs = store.review_for_head(run_id, "e5f6a7b8")
    assert theirs is not None
    assert theirs["status"] == "pending"
    assert not worker.refresh_review_gate()  # skipped is terminal for this head

    (task,) = build_dashboard(store, "sim")["tasks"]
    assert task["review"]["state"] == "skipped"
    assert not task["verified"]


# ------------------------------------------------------------ provider trouble


class Flaky(SimulatedDevinClient):
    """Fails pr-reviews reads for one PR; everything else behaves."""

    def __init__(self, script: list[dict[str, Any]], *, broken: str) -> None:
        super().__init__(script=script)
        self.broken = broken
        self.error = DevinError("pr-reviews returned 503", retryable=True)

    def get_pr_review(
        self, pr_url: str, commit_sha: str | None = None
    ) -> PullRequestReview | None:
        if pr_url == self.broken:
            raise self.error
        return super().get_pr_review(pr_url, commit_sha)


def test_provider_errors_are_recorded_and_give_up_after_the_budget(
    store: Store, quick: Settings
) -> None:
    devin = Flaky(load_script("simulated_session_events.json"), broken=PR_URL)
    worker = Worker(store, quick, devin, FakeSlackTransport(), owner="w")
    intake = Intake(store, quick)
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_success.json", run_id)

    for _ in range(quick.review_max_attempts):
        worker.tick()  # never raises
    row = store.review_for_head(run_id, HEAD)
    assert row is not None
    assert row["status"] == "unavailable"
    assert row["attempts"] == quick.review_max_attempts
    assert "503" in str(row["last_error"])
    assert not worker.refresh_review_gate()  # budget spent, stop calling

    board = build_dashboard(store, "sim")
    (task,) = board["tasks"]
    assert task["checks_passed"]
    assert not task["verified"]
    assert task["review"]["state"] == "unavailable"
    (item,) = board["attention"]
    assert item["reason"] == "review_unavailable"
    assert "pr-reviews API" in item["next_action"]
    assert board["review_gate"]["states"] == {"unavailable": 1}

    # The verdict can still come from GitHub, and then it counts.
    send(intake, "pull_request_review", "devin_review_clean.json", run_id)
    (task,) = build_dashboard(store, "sim")["tasks"]
    assert task["verified"]
    assert task["review"]["state"] == "clear"


def test_a_non_retryable_error_is_unavailable_at_once(
    store: Store, quick: Settings
) -> None:
    devin = Flaky(load_script("simulated_session_events.json"), broken=PR_URL)
    devin.error = DevinError("not authorized (403)", retryable=False, status_code=403)
    worker = Worker(store, quick, devin, FakeSlackTransport(), owner="w")
    run_id = run_to_pr(Intake(store, quick), worker, store)

    worker.tick()
    row = store.review_for_head(run_id, HEAD)
    assert row is not None
    assert row["status"] == "unavailable"
    assert row["attempts"] == 1
    assert _events(store, run_id) == ["unavailable"]


def test_one_broken_review_does_not_stop_another_run(
    store: Store, quick: Settings
) -> None:
    devin = Flaky(load_script("simulated_session_events.json"), broken=PR_URL)
    worker = Worker(store, quick, devin, FakeSlackTransport(), owner="w")
    intake = Intake(store, quick)
    first = run_to_pr(intake, worker, store)

    # A second issue, whose PR lives at another URL.
    issue = load_fixture("issue_labeled.json")
    issue["issue"]["number"] = 4243
    issue["issue"]["title"] = "second"
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=issue
    )
    second = str(result.run_id)
    for _ in range(10):
        worker.tick()
    pr = substitute_run_id(load_fixture("pr_opened.json"), second)
    pr["pull_request"]["number"] = 92
    pr["pull_request"]["html_url"] = PR_URL.replace("/91", "/92")
    pr["pull_request"]["body"] = pr["pull_request"]["body"].replace("#4242", "#4243")
    pr["pull_request"]["head"]["ref"] = f"devin/issue-4243-{second}"
    assert intake.handle(
        delivery_id=next_delivery_id(), event="pull_request", payload=pr
    ).accepted

    for _ in range(quick.review_max_attempts + 5):
        worker.tick()
    broken = store.review_for_head(first, HEAD)
    healthy = store.review_for_head(second, HEAD)
    assert broken is not None
    assert broken["status"] == "unavailable"
    assert healthy is not None
    assert healthy["status"] == "completed"
    assert devin.review_requests == [(PR_URL.replace("/91", "/92"), HEAD)]


# --------------------------------------------------------------------- modes


def test_mode_off_makes_no_calls_and_verifies_on_checks_alone(
    store: Store, settings: Settings, devin: SimulatedDevinClient
) -> None:
    off = dataclasses.replace(settings, review_gate_mode="off")
    worker = Worker(store, off, devin, FakeSlackTransport(), owner="w")
    intake = Intake(store, off)
    run_id = run_to_pr(intake, worker, store)
    worker.tick()
    assert devin.review_requests == []

    send(intake, "check_suite", "check_suite_success.json", run_id)
    board = build_dashboard(store, "sim", review_gate_mode="off")
    (task,) = board["tasks"]
    assert task["verified"]
    assert task["review"]["state"] == "off"
    assert board["results"]["verified"] == 1
    assert "verified" in [e["kind"] for e in store.events_for_run(run_id)]


def test_mode_advisory_requests_and_shows_but_does_not_gate(
    store: Store, settings: Settings, devin: SimulatedDevinClient
) -> None:
    advisory = dataclasses.replace(
        settings, review_gate_mode="advisory", review_poll_seconds=0
    )
    worker = Worker(store, advisory, devin, FakeSlackTransport(), owner="w")
    intake = Intake(store, advisory)
    run_id = run_to_pr(intake, worker, store)
    worker.tick()
    assert devin.review_requests == [(PR_URL, HEAD)]

    send(intake, "check_suite", "check_suite_success.json", run_id)
    board = build_dashboard(store, "sim", review_gate_mode="advisory")
    (task,) = board["tasks"]
    assert task["verified"]
    assert task["review"]["state"] == "pending"
    assert task["review"]["satisfied"]  # advisory never withholds

    # Findings are still shown and still draw attention.
    send(intake, "pull_request_review", "devin_review_findings.json", run_id)
    board = build_dashboard(store, "sim", review_gate_mode="advisory")
    (task,) = board["tasks"]
    assert task["verified"]
    assert task["review"]["state"] == "findings"
    assert task["review"]["findings"] == 2
    assert board["results"]["verified"] == 1


def test_findings_hold_the_gate_and_count_by_kind(
    intake: Intake, worker: Worker, store: Store
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_success.json", run_id)
    send(intake, "pull_request_review_comment", "devin_review_comment.json", run_id)
    send(intake, "pull_request_review", "devin_review_findings.json", run_id)
    # A human's inline comment is not a finding.
    human = substitute_run_id(load_fixture("devin_review_comment.json"), run_id)
    human["comment"]["id"] = 6102
    human["comment"]["user"] = {"login": "SoniaLei"}
    human["comment"]["body"] = "nit: rename this"
    assert not intake.handle(
        delivery_id=next_delivery_id(),
        event="pull_request_review_comment",
        payload=human,
    ).accepted

    board = build_dashboard(store, "sim")
    (task,) = board["tasks"]
    assert task["checks_passed"]
    assert not task["verified"]
    assert task["review"]["state"] == "findings"
    assert task["review"]["findings"] == 2
    assert task["review"]["findings_by_kind"] == {"security": 1}
    assert task["pr"]["review_state"] is None
    (item,) = board["attention"]
    assert item["reason"] == "review_findings"
    assert "dismiss" in item["next_action"]

    row = store.review_for_head(run_id, HEAD)
    assert row is not None
    assert json.loads(str(row["findings_by_kind"])) == {"security": 1}

    # A new commit is re-reviewed from scratch; the findings stay with a1b2c3d4.
    payload = substitute_run_id(load_fixture("pr_opened.json"), run_id)
    payload["action"] = "synchronize"
    payload["pull_request"]["head"]["sha"] = "e5f6a7b8"
    intake.handle(delivery_id=next_delivery_id(), event="pull_request", payload=payload)
    (task,) = build_dashboard(store, "sim")["tasks"]
    assert task["review"]["state"] == "not_requested"
    assert task["review"]["findings_by_kind"] == {}


# ---------------------------------------------------------------- live client

ORG = "org-test"
BASE = f"https://api.example/v3/organizations/{ORG}"


def _client(handler: httpx.MockTransport) -> LiveDevinClient:
    return LiveDevinClient(
        base_url="https://api.example", org_id=ORG, token="cog_test", transport=handler
    )


def test_live_request_posts_only_the_pr_url() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "commit_sha": HEAD,
                "created_at": "2026-09-26T10:00:00Z",
                "pr_number": 91,
                "repo_path": "SoniaLei/superset-cognition-demo",
                "status": "pending",
            },
        )

    review = _client(httpx.MockTransport(handler)).request_pr_review(PR_URL)

    assert seen == {
        "method": "POST",
        "url": f"{BASE}/pr-reviews",
        "body": {"pr_url": PR_URL},
    }
    assert review == PullRequestReview(
        pr_url=PR_URL,
        commit_sha=HEAD,
        status="pending",
        pr_number=91,
        repo_path="SoniaLei/superset-cognition-demo",
        created_at="2026-09-26T10:00:00Z",
    )


def test_live_read_asks_for_the_exact_commit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/pr-reviews")
        assert dict(request.url.params) == {"pr_url": PR_URL, "commit_sha": HEAD}
        return httpx.Response(
            200,
            json={"commit_sha": HEAD, "pr_number": 91, "status": "running"},
        )

    review = _client(httpx.MockTransport(handler)).get_pr_review(PR_URL, HEAD)
    assert review is not None
    assert (review.commit_sha, review.status) == (HEAD, "running")


def test_live_read_treats_404_as_no_review_and_keeps_other_errors() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    assert _client(httpx.MockTransport(missing)).get_pr_review(PR_URL, HEAD) is None

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    with pytest.raises(DevinError) as excinfo:
        _client(httpx.MockTransport(down)).get_pr_review(PR_URL, HEAD)
    assert excinfo.value.retryable
    assert excinfo.value.status_code == 503

    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="no")

    with pytest.raises(DevinError) as excinfo:
        _client(httpx.MockTransport(forbidden)).request_pr_review(PR_URL)
    assert not excinfo.value.retryable
    assert excinfo.value.status_code == 403
