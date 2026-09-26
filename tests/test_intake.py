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
