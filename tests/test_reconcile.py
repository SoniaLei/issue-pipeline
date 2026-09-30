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
"""GitHub reconciliation (D-041): what the API repairs when webhooks were lost,
and what it must never do on its own."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.config import ConfigError, load_settings, Settings
from app.devin_client import SimulatedDevinClient
from app.github_client import (
    GitHubError,
    LiveGitHubClient,
    SimulatedGitHubClient,
)
from app.intake import Intake
from app.prompts import branch_name
from app.reconcile import MAX_ISSUE_EVENTS, Reconciler
from app.slack_client import FakeSlackTransport
from app.states import State
from app.store import Store, utcnow
from app.worker import Worker
from tests.conftest import deliver, load_fixture, substitute_run_id

REPO = "SoniaLei/superset-cognition-demo"
ISSUE = 4242
T0 = "2026-09-20T10:00:00Z"
T1 = "2026-09-20T11:00:00Z"


@pytest.fixture
def github() -> SimulatedGitHubClient:
    return SimulatedGitHubClient()


@pytest.fixture
def reconciler(
    store: Store, settings: Settings, intake: Intake, github: SimulatedGitHubClient
) -> Reconciler:
    return Reconciler(store, settings, intake, github)


def issue_snapshot(**overrides: Any) -> dict[str, Any]:
    issue = dict(load_fixture("issue_labeled.json")["issue"])
    issue["updated_at"] = T0
    issue.update(overrides)
    return issue


def labels(*names: str) -> list[dict[str, str]]:
    return [{"name": name} for name in names]


def pull_snapshot(run_id: str, **overrides: Any) -> dict[str, Any]:
    pull = substitute_run_id(load_fixture("pr_opened.json"), run_id)["pull_request"]
    pull["user"] = {"login": "devin-ai-integration[bot]"}
    pull.update(overrides)
    return pull


def suite(
    suite_id: int, conclusion: str | None = "success", updated_at: str = T0
) -> dict[str, Any]:
    return {
        "id": suite_id,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "updated_at": updated_at,
        "app": {"slug": "github-actions"},
        "url": f"https://api.github.com/repos/{REPO}/check-suites/{suite_id}",
    }


def queued_run(intake: Intake) -> str:
    result = deliver(
        intake, "issues", "issue_labeled.json", issue=issue_snapshot(updated_at=T0)
    )
    assert result.run_id
    return str(result.run_id)


def running_run(intake: Intake, store: Store) -> str:
    run_id = queued_run(intake)
    with store.transaction() as conn:
        store.update_run(
            conn,
            run_id,
            state=State.RUNNING.value,
            branch=branch_name(ISSUE, run_id),
            session_id="devin-sim-0001",
            session_url="https://app.devin.ai/sessions/devin-sim-0001",
        )
    return run_id


def pr_open_run(intake: Intake, store: Store) -> str:
    run_id = running_run(intake, store)
    payload = substitute_run_id(load_fixture("pr_opened.json"), run_id)
    assert intake.handle(
        delivery_id="delivery-pr", event="pull_request", payload=payload
    ).accepted
    return run_id


def run_state(store: Store, run_id: str) -> str:
    run = store.get_run(run_id)
    assert run is not None
    return str(run["state"])


def notification_kinds(store: Store) -> list[str]:
    return [str(row["kind"]) for row in store.all_notifications()]


# --------------------------------------------------------------------- issue


def test_missed_issue_close_cancels_a_run_that_has_not_spent(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = queued_run(intake)
    github.set_issue(
        REPO,
        issue_snapshot(state="closed", updated_at=T1, closed_by={"login": "SoniaLei"}),
    )

    assert reconciler.reconcile_one() is True

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.CANCELLED.value
    assert run["failure_reason"] == "issue_closed"
    task = store.find_task(REPO, ISSUE)
    assert task is not None
    assert task["issue_state"] == "closed"
    assert task["issue_updated_at"] == T1
    assert store.delivery_seen(f"reconcile:{REPO}:issues:{ISSUE}:closed:{T1}")


def test_missed_label_removal_revokes_before_spend(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = queued_run(intake)
    github.set_issue(
        REPO,
        issue_snapshot(labels=labels("bug"), updated_at=T1),
        events=[
            {
                "id": 9001,
                "event": "unlabeled",
                "label": {"name": "devin-ready"},
                "actor": {"login": "SoniaLei"},
            }
        ],
    )

    reconciler.reconcile_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.CANCELLED.value
    assert run["approval_revoked_by"] == "SoniaLei"


def test_a_label_the_api_shows_does_not_approve_without_its_actor(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    """The issue object says `devin-ready` is present. That is not a
    decision by anyone (D-001, D-038): without the `labeled` event naming
    an allowlisted actor, the run stays awaiting approval."""
    result = deliver(
        intake,
        "issues",
        "issue_opened.json",
        issue=issue_snapshot(labels=labels("bug"), updated_at=T0),
    )
    awaiting = store.active_run_for_task(int(result.task_id))
    assert awaiting is not None
    run_id = str(awaiting["id"])
    assert run_state(store, run_id) == State.AWAITING_APPROVAL.value

    github.set_issue(REPO, issue_snapshot(updated_at=T1), events=[])
    reconciler.reconcile_one()
    assert run_state(store, run_id) == State.AWAITING_APPROVAL.value

    # Applied by someone outside the allowlist: recorded, still not spend.
    github.set_issue(
        REPO,
        issue_snapshot(updated_at=T1),
        events=[
            {
                "id": 9002,
                "event": "labeled",
                "label": {"name": "devin-ready"},
                "actor": {"login": "drive-by"},
            }
        ],
    )
    with store.transaction() as conn:
        store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
    reconciler.reconcile_one()
    assert run_state(store, run_id) == State.AWAITING_APPROVAL.value

    # An event log too long to read whole (oldest first, so the newest label
    # events are the unread ones) names nobody, even if an old entry would.
    maintainer_labeled = {
        "id": 9003,
        "event": "labeled",
        "label": {"name": "devin-ready"},
        "actor": {"login": "SoniaLei"},
    }
    padding = [{"id": 1000 + i, "event": "mentioned"} for i in range(MAX_ISSUE_EVENTS)]
    github.set_issue(
        REPO, issue_snapshot(updated_at=T1), events=[maintainer_labeled, *padding]
    )
    with store.transaction() as conn:
        store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
    reconciler.reconcile_one()
    assert run_state(store, run_id) == State.AWAITING_APPROVAL.value

    # Applied by a maintainer: the same decision a webhook would have carried.
    github.set_issue(REPO, issue_snapshot(updated_at=T1), events=[maintainer_labeled])
    with store.transaction() as conn:
        store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
    reconciler.reconcile_one()
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.QUEUED.value
    assert run["approved_by"] == "SoniaLei"


def test_an_older_api_snapshot_is_stale_under_d040(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    """Cannot happen against a live API, but the guard is the same one
    webhooks get: a snapshot older than the recorded one changes nothing."""
    run_id = queued_run(intake)
    github.set_issue(
        REPO,
        issue_snapshot(state="closed", updated_at="2026-09-19T00:00:00Z"),
    )
    reconciler.reconcile_one()
    assert run_state(store, run_id) == State.QUEUED.value


# ---------------------------------------------------------------------- pull


def test_missed_pr_open_is_found_on_the_run_branch(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = running_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    github.set_pull(REPO, pull_snapshot(run_id), check_suites=[suite(7001)])

    reconciler.reconcile_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.PR_OPEN.value
    assert run["pr_number"] == 91
    assert run["head_sha"] == "a1b2c3d4"
    assert run["checks_state"] == "passed"
    assert notification_kinds(store) == ["pr_opened"]
    assert ("pulls_for_branch", REPO, branch_name(ISSUE, run_id)) in github.calls


def test_a_pr_on_another_branch_is_never_discovered(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    """Discovery asks GitHub for the run's own branch only; a PR elsewhere
    on the repo (marker or not) is not this run's, so it is never replayed."""
    run_id = running_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    pull = pull_snapshot(run_id, number=77)
    pull["head"] = {**pull["head"], "ref": "feature/unrelated"}
    github.set_pull(REPO, pull)

    reconciler.reconcile_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.RUNNING.value
    assert run["pr_number"] is None
    assert notification_kinds(store) == []


def test_missed_merge_lands_the_run_terminal(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    github.set_pull(
        REPO,
        pull_snapshot(
            run_id,
            state="closed",
            merged=True,
            merge_commit_sha="feedface00",
            merged_by={"login": "SoniaLei"},
        ),
    )

    reconciler.reconcile_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.MERGED.value
    assert run["merged_sha"] == "feedface00"
    assert notification_kinds(store)[-1] == "pr_merged"


def test_missed_close_without_merge_is_not_success(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    github.set_pull(REPO, pull_snapshot(run_id, state="closed", merged=False))
    reconciler.reconcile_one()
    assert run_state(store, run_id) == State.CLOSED_UNMERGED.value


def test_a_pr_opened_and_closed_while_down_lands_terminal(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    """Discovery finds the PR already merged: it opened, then closed, and
    the run must end where GitHub says it did, not at `pr_open`."""
    run_id = running_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    github.set_pull(
        REPO,
        pull_snapshot(
            run_id,
            state="closed",
            merged=True,
            merge_commit_sha="feedface00",
            merged_by={"login": "SoniaLei"},
        ),
        check_suites=[suite(7001)],
    )

    reconciler.reconcile_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.MERGED.value
    assert run["pr_number"] == 91
    assert run["merged_sha"] == "feedface00"
    assert run["checks_state"] == "passed"
    assert notification_kinds(store)[-2:] == ["pr_opened", "pr_merged"]


def test_a_late_merge_still_records_the_final_head_and_its_evidence(
    intake: Intake,
    store: Store,
    settings: Settings,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    """A PR that moved to a new head, passed its checks and merged while the
    service was down: the terminal run keeps what GitHub judged it on."""
    run_id = pr_open_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    github.set_pull(
        REPO,
        pull_snapshot(
            run_id,
            state="closed",
            merged=True,
            merge_commit_sha="feedface00",
            head={
                "ref": branch_name(ISSUE, run_id),
                "sha": "e5f6a7b8",
                "repo": {"full_name": REPO},
            },
        ),
        check_suites=[suite(7001), suite(7002)],
    )

    due = store.reconcile_candidate(settings.env, "9999")
    assert due is not None
    applied = reconciler.reconcile_run(due)

    assert applied[:1] == ["pr.synchronize"]
    assert applied[-1] == "pr.merged"
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.MERGED.value
    assert run["head_sha"] == "e5f6a7b8"
    assert run["checks_head_sha"] == "e5f6a7b8"
    assert run["checks_state"] == "passed"
    assert len(store.checks_for_head(run_id, "e5f6a7b8")) == 2


def test_draft_toggles_on_one_head_are_each_replayed(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    for draft, stamp in ((True, T0), (False, T1), (True, "2026-09-20T12:00:00Z")):
        github.set_pull(REPO, pull_snapshot(run_id, draft=draft, updated_at=stamp))
        with store.transaction() as conn:
            store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
        reconciler.reconcile_one()
        run = store.get_run(run_id)
        assert run is not None
        assert bool(run["pr_draft"]) is draft


def test_a_new_head_invalidates_checks_and_review_evidence(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    deliver(
        intake,
        "check_suite",
        "check_suite_success.json",
        check_suite={**load_fixture("check_suite_success.json")["check_suite"]},
    )
    before = store.get_run(run_id)
    assert before is not None
    assert before["checks_state"] == "passed"

    github.set_issue(REPO, issue_snapshot())
    new_head = {"ref": branch_name(ISSUE, run_id), "sha": "e5f6a7b8"}
    github.set_pull(
        REPO,
        pull_snapshot(run_id, head={**new_head, "repo": {"full_name": REPO}}),
        check_suites=[suite(7002, conclusion=None)],
    )

    reconciler.reconcile_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["head_sha"] == "e5f6a7b8"
    assert run["checks_head_sha"] == "e5f6a7b8"
    assert run["checks_state"] == "pending"
    assert store.review_for_head(run_id, "e5f6a7b8") is None
    # The suite belongs to the new head only.
    assert [row["head_sha"] for row in store.checks_for_head(run_id, "e5f6a7b8")] == [
        "e5f6a7b8"
    ]


def test_failed_checks_read_from_the_api_notify_once(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    github.set_pull(
        REPO,
        pull_snapshot(run_id),
        check_suites=[suite(7001), suite(7002, conclusion="failure")],
    )
    reconciler.reconcile_one()
    run = store.get_run(run_id)
    assert run is not None
    assert run["checks_state"] == "failed"
    assert notification_kinds(store).count("checks_failed") == 1

    # Same API answer again: nothing new to record, nothing new to say.
    with store.transaction() as conn:
        store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
    reconciler.reconcile_one()
    assert notification_kinds(store).count("checks_failed") == 1
    kinds = [row["kind"] for row in store.events_for_run(run_id)]
    assert kinds.count("reconcile") == 1


def test_a_partial_read_never_verifies_a_head(
    store: Store,
    settings: Settings,
    github: SimulatedGitHubClient,
) -> None:
    """GitHub answers with the whole suite list at once; fed to the intake
    one by one, the passing suite must not be judged before the failing one."""
    ungated = replace(settings, review_gate_mode="off")
    intake = Intake(store, ungated)
    reconciler = Reconciler(store, ungated, intake, github)
    run_id = pr_open_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    github.set_pull(
        REPO,
        pull_snapshot(run_id),
        check_suites=[suite(7001), suite(7002, conclusion="failure"), suite(7003)],
    )

    reconciler.reconcile_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["checks_state"] == "failed"
    kinds = notification_kinds(store)
    assert "verified" not in kinds
    assert "verified" not in [row["kind"] for row in store.events_for_run(run_id)]
    assert kinds.count("checks_failed") == 1


def test_a_rerun_suite_is_followed_through_pending_back_to_success(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    states: list[str] = []
    for suites in (
        [suite(7001, updated_at=T0)],
        [suite(7001, conclusion=None, updated_at=T1)],
        [suite(7001, updated_at="2026-09-20T12:00:00Z")],
    ):
        github.set_pull(REPO, pull_snapshot(run_id), check_suites=suites)
        with store.transaction() as conn:
            store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
        reconciler.reconcile_one()
        run = store.get_run(run_id)
        assert run is not None
        states.append(str(run["checks_state"]))
    assert states == ["passed", "pending", "passed"]


def test_reviews_are_replayed_for_bot_verdicts_and_the_latest_human_verdict(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    bot_review = load_fixture("devin_review_findings.json")["review"]
    github.set_issue(REPO, issue_snapshot())
    github.set_pull(
        REPO,
        pull_snapshot(run_id),
        check_suites=[suite(7001)],
        reviews=[
            {**bot_review, "state": "COMMENTED"},
            {
                "id": 5000,
                "state": "CHANGES_REQUESTED",
                "commit_id": "a1b2c3d4",
                "html_url": f"https://github.com/{REPO}/pull/91#pullrequestreview-5000",
                "user": {"login": "SoniaLei"},
            },
            {
                "id": 5001,
                "state": "APPROVED",
                "commit_id": "a1b2c3d4",
                "html_url": f"https://github.com/{REPO}/pull/91#pullrequestreview-5001",
                "user": {"login": "SoniaLei"},
            },
            {
                "id": 5002,
                "state": "COMMENTED",
                "commit_id": "a1b2c3d4",
                "html_url": f"https://github.com/{REPO}/pull/91#pullrequestreview-5002",
                "user": {"login": "someone"},
            },
        ],
    )

    reconciler.reconcile_one()

    run = store.get_run(run_id)
    assert run is not None
    assert run["review_state"] == "approved"
    review = store.review_for_head(run_id, "a1b2c3d4")
    assert review is not None
    assert review["findings"] == bot_review_findings(bot_review)
    reasons = [
        (row["kind"], row["reason"])
        for row in store.events_for_run(run_id)
        if row["kind"] in {"review", "review_gate"}
    ]
    assert reasons == [("review_gate", "findings"), ("review", "approved")]

    # Read again: the verdicts are known, nothing is replayed.
    with store.transaction() as conn:
        store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
    reconciler.reconcile_one()
    assert notification_kinds(store).count("human_review") == 1
    assert notification_kinds(store).count("review_findings") == 1


def bot_review_findings(review: dict[str, Any]) -> int:
    from app.review_gate import parse_summary

    findings = parse_summary(review.get("body"))
    assert findings is not None
    return findings


# -------------------------------------------------------------- protection


def test_required_checks_are_recorded_beside_the_suites_not_as_the_gate(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    # One required context, one optional suite reported green.
    github.set_pull(REPO, pull_snapshot(run_id), check_suites=[suite(7001)])
    github.set_branch(REPO, "master", required_checks=["ci / unit", "ci / lint"])

    reconciler.reconcile_one()

    run = store.get_run(run_id)
    assert run is not None
    assert json.loads(run["required_checks"]) == ["ci / lint", "ci / unit"]
    # Verification is still "every suite GitHub reported" (D-030): the
    # required list is an observation and does not narrow the verdict.
    assert run["checks_state"] == "passed"
    protection = [e for e in store.events_for_run(run_id) if e["kind"] == "protection"]
    assert len(protection) == 1
    assert json.loads(protection[0]["detail"])["base"] == "master"

    # Unchanged protection is not re-recorded; a change is.
    with store.transaction() as conn:
        store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
    reconciler.reconcile_one()
    assert (
        len([e for e in store.events_for_run(run_id) if e["kind"] == "protection"]) == 1
    )

    github.set_branch(REPO, "master", required_checks=["ci / unit"])
    with store.transaction() as conn:
        store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
    reconciler.reconcile_one()
    run = store.get_run(run_id)
    assert run is not None
    assert json.loads(run["required_checks"]) == ["ci / unit"]


def test_an_unprotected_or_unknown_base_requires_nothing(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    github.set_issue(REPO, issue_snapshot())
    github.set_pull(REPO, pull_snapshot(run_id))
    # No branch object at all: GitHub answers 404, which is "unprotected".
    reconciler.reconcile_one()
    run = store.get_run(run_id)
    assert run is not None
    assert run["required_checks"] is None
    assert ("branch", REPO, "master") in github.calls
    assert not [e for e in store.events_for_run(run_id) if e["kind"] == "protection"]

    # Protected, but no required status checks: recorded as an empty list only
    # once something was required before.
    github.set_branch(REPO, "master", required_checks=[])
    with store.transaction() as conn:
        store.mark_reconciled(conn, run_id, "2000-01-01T00:00:00+00:00")
    reconciler.reconcile_one()
    run = store.get_run(run_id)
    assert run is not None
    assert run["required_checks"] is None


# ---------------------------------------------------------- driver & bounds


def test_terminal_runs_are_never_candidates(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = pr_open_run(intake, store)
    payload = substitute_run_id(load_fixture("pr_merged.json"), run_id)
    intake.handle(delivery_id="delivery-merged", event="pull_request", payload=payload)
    assert run_state(store, run_id) == State.MERGED.value

    # GitHub now says the PR is open again (it cannot, but the API is data).
    github.set_issue(REPO, issue_snapshot(updated_at=T1))
    github.set_pull(REPO, pull_snapshot(run_id, state="open"))
    assert reconciler.reconcile_one() is False
    assert github.calls == []
    assert run_state(store, run_id) == State.MERGED.value


def test_reads_are_spaced_by_the_interval_and_marked(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
    settings: Settings,
) -> None:
    run_id = queued_run(intake)
    github.set_issue(REPO, issue_snapshot())
    assert reconciler.reconcile_one() is True
    run = store.get_run(run_id)
    assert run is not None
    assert run["reconciled_at"] is not None
    assert reconciler.reconcile_one() is False
    later = utcnow() + timedelta(seconds=settings.reconcile_interval_seconds + 1)
    assert reconciler.reconcile_one(now=later) is True
    assert len([c for c in github.calls if c[0] == "issue"]) == 2


def test_catch_up_reads_every_active_run_once(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    first = queued_run(intake)
    second = deliver(
        intake,
        "issues",
        "issue_labeled.json",
        issue=issue_snapshot(number=4300, updated_at=T0),
    ).run_id
    github.set_issue(REPO, issue_snapshot())
    github.set_issue(REPO, issue_snapshot(number=4300))
    with store.transaction() as conn:
        store.mark_reconciled(conn, first, utcnow().isoformat())

    assert reconciler.catch_up() == 2
    assert sorted(c[2] for c in github.calls if c[0] == "issue") == ["4242", "4300"]
    assert str(second) != first
    # Both were just read: the interval pass has nothing due.
    assert reconciler.reconcile_one() is False


def test_an_api_failure_is_recorded_and_does_not_pin_the_reconciler(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    broken = queued_run(intake)
    healthy = deliver(
        intake,
        "issues",
        "issue_labeled.json",
        issue=issue_snapshot(number=4300, updated_at=T0),
    ).run_id
    github.set_issue(REPO, issue_snapshot(number=4300))
    # No issue 4242 in the simulated API: the read raises a GitHubError.

    assert reconciler.reconcile_one() is True
    beats = {row["component"]: row for row in store.heartbeats()}
    assert "issue" in str(beats["reconciler"]["detail"])
    run = store.get_run(broken)
    assert run is not None
    assert run["reconciled_at"] is not None
    assert run["state"] == State.QUEUED.value

    assert reconciler.reconcile_one() is True
    assert ("issue", REPO, "4300") in github.calls
    assert run_state(store, str(healthy)) == State.QUEUED.value


def test_worker_reconciles_on_busy_ticks_too_and_survives_errors(
    intake: Intake,
    store: Store,
    settings: Settings,
    github: SimulatedGitHubClient,
    devin: SimulatedDevinClient,
    slack: FakeSlackTransport,
) -> None:
    """A session being polled keeps every tick busy; its own issue must
    still be re-read, or a revocation could go unseen for the session's
    whole life."""
    first = queued_run(intake)
    second = deliver(
        intake,
        "issues",
        "issue_labeled.json",
        issue=issue_snapshot(number=4300, updated_at=T0),
    ).run_id
    github.set_issue(REPO, issue_snapshot())
    github.set_issue(REPO, issue_snapshot(number=4300, state="closed", updated_at=T1))
    with store.transaction() as conn:
        store.mark_reconciled(conn, first, "2000-01-01T00:00:00+00:00")
    worker = Worker(
        store,
        settings,
        devin,
        slack,
        owner="test-worker",
        reconciler=Reconciler(store, settings, intake, github),
    )
    # A busy tick: the first run starts a session, and GitHub is still read
    # (never-read runs first), so the second issue's close is seen before
    # that run can spend.
    assert worker.tick() is True
    assert ("issue", REPO, "4300") in github.calls
    assert run_state(store, first) != State.QUEUED.value
    assert run_state(store, str(second)) == State.CANCELLED.value

    class Exploding(SimulatedGitHubClient):
        def get_issue(self, repo: str, number: int) -> dict[str, Any]:
            raise RuntimeError("boom")

    worker.reconciler = Reconciler(store, settings, intake, Exploding())
    with store.transaction() as conn:
        store.mark_reconciled(conn, first, "2000-01-01T00:00:00+00:00")
    assert worker.tick() is True  # the error is logged, not raised
    assert worker.catch_up_github() == 0

    without = Worker(store, settings, devin, slack, owner="test-worker-2")
    assert without.reconcile_github() is False
    assert without.catch_up_github() == 0


def test_a_no_op_read_leaves_updated_at_alone(
    intake: Intake,
    store: Store,
    github: SimulatedGitHubClient,
    reconciler: Reconciler,
) -> None:
    run_id = queued_run(intake)
    before = store.get_run(run_id)
    assert before is not None
    github.set_issue(REPO, issue_snapshot())
    reconciler.reconcile_one()
    after = store.get_run(run_id)
    assert after is not None
    assert after["updated_at"] == before["updated_at"]
    assert [row["kind"] for row in store.events_for_run(run_id)].count("reconcile") == 0


# -------------------------------------------------------------- live client


def _client(handler: httpx.MockTransport) -> LiveGitHubClient:
    return LiveGitHubClient(
        base_url="https://api.example", token="ghp_test", transport=handler
    )


def test_live_client_sends_read_only_gets_with_the_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"number": 4242, "state": "open"})

    client = _client(httpx.MockTransport(handler))
    assert client.get_issue(REPO, 4242)["state"] == "open"
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == f"/repos/{REPO}/issues/4242"
    assert request.headers["Authorization"] == "Bearer ghp_test"
    assert request.headers["Accept"] == "application/vnd.github+json"


def test_live_client_follows_link_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"check_suites": [{"id": 2}]})
        return httpx.Response(
            200,
            json={"check_suites": [{"id": 1}]},
            headers={
                "Link": (
                    f"<https://api.example/repos/{REPO}/commits/abc/check-suites"
                    '?page=2>; rel="next"'
                )
            },
        )

    client = _client(httpx.MockTransport(handler))
    assert [s["id"] for s in client.list_check_suites(REPO, "abc")] == [1, 2]


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(401, False), (403, False), (404, False), (429, True), (502, True)],
)
def test_live_client_classifies_errors(status: int, retryable: bool) -> None:
    client = _client(
        httpx.MockTransport(lambda request: httpx.Response(status, text="nope"))
    )
    with pytest.raises(GitHubError) as info:
        client.get_pull(REPO, 1)
    assert info.value.retryable is retryable
    assert info.value.status_code == status


def test_live_github_mode_requires_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("GITHUB_MODE", "live")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        load_settings()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    assert load_settings().github_mode == "live"
    monkeypatch.setenv("GITHUB_MODE", "off")
    assert load_settings().github_mode == "off"


def test_simulated_client_is_data_only() -> None:
    github = SimulatedGitHubClient()
    with pytest.raises(GitHubError) as info:
        github.get_issue(REPO, 1)
    assert info.value.retryable is False
    assert json.dumps(github.list_issue_events(REPO, 1)) == "[]"
