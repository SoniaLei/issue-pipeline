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
"""Intake: signatures, deduplication and the authorization gate."""

from __future__ import annotations

import hashlib
import hmac
import json

from app.config import Settings
from app.intake import Intake, verify_signature
from app.states import State
from app.store import Store
from tests.conftest import deliver, load_fixture, next_delivery_id


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_accepts_only_the_exact_body() -> None:
    body = b'{"action":"opened"}'
    header = sign("test-secret", body)
    assert verify_signature("test-secret", body, header)
    # A re-serialised body is a different body, which is why the raw bytes are
    # what gets signed.
    assert not verify_signature("test-secret", b'{"action": "opened"}', header)
    assert not verify_signature("other-secret", body, header)
    assert not verify_signature("test-secret", body, None)
    assert not verify_signature("test-secret", body, "md5=whatever")


def test_duplicate_delivery_is_ignored(intake: Intake, store: Store) -> None:
    payload = load_fixture("issue_labeled.json")
    delivery = next_delivery_id()
    first = intake.handle(delivery_id=delivery, event="issues", payload=payload)
    second = intake.handle(delivery_id=delivery, event="issues", payload=payload)

    assert first.accepted
    assert not second.accepted
    assert second.reason == "duplicate delivery"
    task = store.find_task(str(payload["repository"]["full_name"]), 4242)
    assert task is not None
    assert len(store.runs_for_task(int(task["id"]))) == 1


def test_issue_opened_awaits_approval(intake: Intake, store: Store) -> None:
    result = deliver(intake, "issues", "issue_opened.json")
    assert result.accepted
    assert result.task_id is not None
    run = store.active_run_for_task(result.task_id)
    assert run is not None
    assert run["state"] == State.AWAITING_APPROVAL.value
    assert run["approved_by"] is None


def test_label_from_an_unauthorized_actor_does_not_queue(
    intake: Intake, store: Store
) -> None:
    result = deliver(intake, "issues", "issue_labeled_untrusted.json")
    assert not result.accepted
    assert "not an authorized approver" in result.reason
    task = store.find_task("SoniaLei/superset-cognition-demo", 4242)
    assert task is not None
    assert store.active_run_for_task(int(task["id"])) is None


def test_label_from_a_maintainer_queues_a_run(intake: Intake, store: Store) -> None:
    result = deliver(intake, "issues", "issue_labeled.json")
    assert result.accepted
    run = store.get_run(str(result.run_id))
    assert run is not None
    assert run["state"] == State.QUEUED.value
    assert run["approved_by"] == "SoniaLei"
    assert run["max_acu_limit"] == 20
    # The issue as it stood at approval is kept, so later edits cannot change
    # what was authorized.
    assert json.loads(str(run["input_snapshot"]))["number"] == 4242


def test_opened_with_the_label_already_present_uses_the_same_gate(
    intake: Intake, store: Store
) -> None:
    payload = load_fixture("issue_labeled.json")
    payload["action"] = "opened"
    payload["sender"] = {"login": "drive-by"}
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=payload
    )
    assert not result.accepted


def test_second_label_event_does_not_create_a_second_run(
    intake: Intake, store: Store
) -> None:
    first = deliver(intake, "issues", "issue_labeled.json")
    second = deliver(intake, "issues", "issue_labeled.json")
    assert second.run_id == first.run_id
    assert len(store.runs_for_task(int(first.task_id or 0))) == 1


def test_unlabel_before_execution_cancels(intake: Intake, store: Store) -> None:
    queued = deliver(intake, "issues", "issue_labeled.json")
    deliver(intake, "issues", "issue_unlabeled.json")
    run = store.get_run(str(queued.run_id))
    assert run is not None
    assert run["state"] == State.CANCELLED.value
    assert run["approval_revoked_by"] == "SoniaLei"


def test_unlabel_after_execution_lets_the_session_finish(
    intake: Intake, store: Store
) -> None:
    queued = deliver(intake, "issues", "issue_labeled.json")
    with store.transaction() as conn:
        store.update_run(conn, str(queued.run_id), state=State.RUNNING.value)

    deliver(intake, "issues", "issue_unlabeled.json")

    run = store.get_run(str(queued.run_id))
    assert run is not None
    assert run["state"] == State.RUNNING.value
    assert run["approval_revoked_by"] == "SoniaLei"
    kinds = [row["reason"] for row in store.all_notifications()]
    assert "approval_revoked" in kinds


def test_events_for_other_repositories_are_dropped(
    store: Store, settings: Settings
) -> None:
    intake = Intake(store, settings)
    payload = load_fixture("issue_labeled.json")
    payload["repository"]["full_name"] = "someone/else"
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=payload
    )
    assert not result.accepted
    assert result.reason == "repository not allowlisted"
    assert store.list_tasks() == []


def test_later_events_by_a_maintainer_do_not_adopt_an_untrusted_label(
    intake: Intake, store: Store
) -> None:
    rejected = deliver(intake, "issues", "issue_labeled_untrusted.json")
    assert not rejected.accepted
    for action, label in (("edited", None), ("labeled", "bug")):
        payload = load_fixture("issue_labeled.json")
        payload["action"] = action
        if label is None:
            payload.pop("label", None)
        else:
            payload["label"] = {"name": label}
        result = intake.handle(
            delivery_id=next_delivery_id(), event="issues", payload=payload
        )
        assert "queued" not in result.reason, action
        run = store.active_run_for_task(int(rejected.task_id or 0))
        assert run is None or run["state"] == State.AWAITING_APPROVAL.value, action


def test_reopen_by_a_maintainer_re_approves_a_labelled_issue(
    intake: Intake, store: Store
) -> None:
    queued = deliver(intake, "issues", "issue_labeled.json")
    closed = load_fixture("issue_labeled.json")
    closed["action"] = "closed"
    closed.pop("label", None)
    intake.handle(delivery_id=next_delivery_id(), event="issues", payload=closed)
    first = store.get_run(str(queued.run_id))
    assert first is not None
    assert first["state"] == State.CANCELLED.value

    reopened = load_fixture("issue_labeled.json")
    reopened["action"] = "reopened"
    reopened.pop("label", None)
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=reopened
    )
    assert result.accepted
    assert result.reason == "run queued from reopened"
    run = store.active_run_for_task(int(result.task_id or 0))
    assert run is not None
    assert run["state"] == State.QUEUED.value
    assert run["approved_by"] == "SoniaLei"
    assert run["id"] != first["id"]

    # The same reopen by someone outside the allowlist is recorded, not acted on.
    reopened["sender"] = {"login": "drive-by"}
    with store.transaction() as conn:
        store.update_run(conn, str(run["id"]), state=State.CANCELLED.value)
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=reopened
    )
    assert not result.accepted
    assert store.active_run_for_task(int(result.task_id or 0)) is None


def _issue_event(action: str, updated_at: str | None, **issue: object) -> dict:
    payload = load_fixture("issue_labeled.json")
    payload["action"] = action
    payload.pop("label", None)
    if updated_at is not None:
        payload["issue"]["updated_at"] = updated_at
    payload["issue"].update(issue)
    return payload


def test_a_close_overtaken_by_its_reopen_does_not_cancel(
    intake: Intake, store: Store
) -> None:
    labeled = load_fixture("issue_labeled.json")
    labeled["issue"]["updated_at"] = "2026-09-28T10:00:00Z"
    queued = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=labeled
    )
    assert queued.accepted
    assert queued.run_id

    # GitHub delivers the reopen (10:02) before the close (10:01) it followed.
    reopened = _issue_event("reopened", "2026-09-28T10:02:00Z", state="open")
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=reopened
    )
    assert result.accepted
    late_close = _issue_event("closed", "2026-09-28T10:01:00Z", state="closed")
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=late_close
    )
    assert not result.accepted
    assert result.reason.startswith("stale delivery")

    run = store.get_run(str(queued.run_id))
    assert run is not None
    assert run["state"] == State.QUEUED.value
    task = store.get_task(int(run["task_id"]))
    assert task is not None
    assert task["issue_state"] == "open"
    assert task["issue_updated_at"] == "2026-09-28T10:02:00Z"

    # A close that really is later still cancels.
    close = _issue_event("closed", "2026-09-28T10:03:00Z", state="closed")
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=close
    )
    assert result.accepted
    run = store.get_run(str(queued.run_id))
    assert run is not None
    assert run["state"] == State.CANCELLED.value


def test_a_label_overtaken_by_its_removal_does_not_approve(
    intake: Intake, store: Store
) -> None:
    opened = _issue_event("opened", "2026-09-28T10:00:00Z", labels=[])
    intake.handle(delivery_id=next_delivery_id(), event="issues", payload=opened)

    # `unlabeled` (10:02) arrives first; the `labeled` (10:01) it undid follows.
    unlabeled = _issue_event("unlabeled", "2026-09-28T10:02:00Z", labels=[])
    unlabeled["label"] = {"name": "devin-ready"}
    intake.handle(delivery_id=next_delivery_id(), event="issues", payload=unlabeled)
    labeled = load_fixture("issue_labeled.json")
    labeled["issue"]["updated_at"] = "2026-09-28T10:01:00Z"
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=labeled
    )
    assert not result.accepted
    assert result.reason.startswith("stale delivery")
    assert store.active_run_for_task(int(result.task_id or 0)) is None


def test_deliveries_without_timestamps_are_applied_in_arrival_order(
    intake: Intake, store: Store
) -> None:
    queued = deliver(intake, "issues", "issue_labeled.json")
    closed = _issue_event("closed", None, state="closed")
    assert "updated_at" not in closed["issue"]
    result = intake.handle(
        delivery_id=next_delivery_id(), event="issues", payload=closed
    )
    assert result.accepted
    run = store.get_run(str(queued.run_id))
    assert run is not None
    assert run["state"] == State.CANCELLED.value
