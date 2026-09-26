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
"""Attaching a pull request to the run that is allowed to own it."""

from __future__ import annotations

from typing import Any

from app.intake import Intake
from app.prompts import branch_name, extract_marker, run_marker
from app.states import State
from app.store import Store
from tests.conftest import (
    deliver,
    load_fixture,
    next_delivery_id,
    substitute_run_id,
)


def queued_run(intake: Intake, store: Store) -> str:
    result = deliver(intake, "issues", "issue_labeled.json")
    run_id = str(result.run_id)
    with store.transaction() as conn:
        store.update_run(
            conn,
            run_id,
            state=State.RUNNING.value,
            branch=branch_name(4242, run_id),
            session_url="https://app.devin.ai/sessions/devin-sim-0001",
            session_id="devin-sim-0001",
        )
    return run_id


def deliver_pr(intake: Intake, fixture: str, run_id: str, **overrides: Any) -> Any:
    payload = substitute_run_id(load_fixture(fixture), run_id)
    payload.update(overrides)
    return intake.handle(
        delivery_id=next_delivery_id(), event="pull_request", payload=payload
    )


def test_marker_round_trips() -> None:
    body = f"Fixes #1\n\n{run_marker('abc123')}\n"
    assert extract_marker(body) == "abc123"
    assert extract_marker("no marker here") is None
    assert extract_marker(None) is None


def test_pr_on_the_expected_branch_attaches(intake: Intake, store: Store) -> None:
    run_id = queued_run(intake, store)
    result = deliver_pr(intake, "pr_opened.json", run_id)

    assert result.accepted
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.PR_OPEN.value
    assert run["pr_number"] == 91
    assert run["head_sha"] == "a1b2c3d4"
    assert [row["kind"] for row in store.all_notifications()] == ["pr_opened"]


def test_a_copied_marker_from_a_fork_does_not_attach(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    payload = substitute_run_id(load_fixture("pr_opened_foreign.json"), run_id)
    # The stranger's PR carries the real marker, copied from the tracked PR.
    payload["pull_request"]["body"] = f"{run_marker(run_id)}"
    result = intake.handle(
        delivery_id=next_delivery_id(), event="pull_request", payload=payload
    )

    assert not result.accepted
    run = store.get_run(run_id)
    assert run is not None
    assert run["pr_number"] is None


def test_marker_alone_attaches_when_the_branch_is_not_yet_known(
    intake: Intake, store: Store
) -> None:
    result = deliver(intake, "issues", "issue_labeled.json")
    run_id = str(result.run_id)
    with store.transaction() as conn:
        store.update_run(conn, run_id, state=State.RUNNING.value)

    payload = substitute_run_id(load_fixture("pr_opened.json"), run_id)
    payload["pull_request"]["head"]["ref"] = "some-other-branch"
    attached = intake.handle(
        delivery_id=next_delivery_id(), event="pull_request", payload=payload
    )
    assert attached.accepted


def test_merge_records_the_sha_and_notifies(intake: Intake, store: Store) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    deliver_pr(intake, "pr_merged.json", run_id)

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.MERGED.value
    assert run["merged_sha"] == "ffeeddcc"
    assert [row["kind"] for row in store.all_notifications()] == [
        "pr_opened",
        "pr_merged",
    ]


def test_closing_unmerged_is_not_success(intake: Intake, store: Store) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    deliver_pr(intake, "pr_closed_unmerged.json", run_id)

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.CLOSED_UNMERGED.value
    assert [row["kind"] for row in store.all_notifications()][-1] == "pr_closed"


def test_a_late_delivery_cannot_reopen_a_merged_task(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    deliver_pr(intake, "pr_merged.json", run_id)
    # The `opened` delivery arriving after the merge, as GitHub is entitled to
    # do: it must not walk the task backwards.
    late = deliver_pr(intake, "pr_opened.json", run_id)

    assert late.accepted
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.MERGED.value


def test_a_new_head_invalidates_previous_check_evidence(
    intake: Intake, store: Store
) -> None:
    run_id = queued_run(intake, store)
    deliver_pr(intake, "pr_opened.json", run_id)
    with store.transaction() as conn:
        store.update_run(
            conn, run_id, checks_state="passed", checks_head_sha="a1b2c3d4"
        )

    payload = substitute_run_id(load_fixture("pr_opened.json"), run_id)
    payload["action"] = "synchronize"
    payload["pull_request"]["head"]["sha"] = "99887766"
    intake.handle(delivery_id=next_delivery_id(), event="pull_request", payload=payload)

    run = store.get_run(run_id)
    assert run is not None
    assert run["head_sha"] == "99887766"
    assert run["checks_state"] is None
