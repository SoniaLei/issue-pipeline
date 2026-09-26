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
"""The HTTP surface, and the state machine's own rules."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.reporting import build_report, render_text
from app.states import check_transition, IllegalTransitionError, is_terminal, State
from app.store import Store
from tests.conftest import load_fixture


@pytest.fixture
def client(settings: Settings, store: Store) -> TestClient:
    return TestClient(create_app(settings, store))


def post(
    client: TestClient,
    event: str,
    payload: dict[str, object],
    delivery: str,
    secret: str = "test-secret",
) -> httpx.Response:
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
        },
    )


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["devin_mode"] == "sim"


def test_a_bad_signature_is_rejected_before_anything_is_stored(
    client: TestClient, store: Store
) -> None:
    payload = load_fixture("issue_labeled.json")
    response = post(client, "issues", payload, "d-1", secret="wrong-secret")
    assert response.status_code == 401
    assert store.list_tasks() == []


def test_a_signed_delivery_is_accepted(client: TestClient, store: Store) -> None:
    payload = load_fixture("issue_labeled.json")
    response = post(client, "issues", payload, "d-2")
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert len(store.list_tasks()) == 1


def test_an_unactionable_delivery_is_still_acknowledged(client: TestClient) -> None:
    payload = load_fixture("issue_labeled_untrusted.json")
    response = post(client, "issues", payload, "d-3")
    # 200 on purpose: a 4xx buys redeliveries of an event that will be ignored
    # in exactly the same way each time.
    assert response.status_code == 200
    assert response.json()["accepted"] is False


def test_missing_headers_are_a_client_error(client: TestClient) -> None:
    body = b"{}"
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": signature},
    )
    assert response.status_code == 400


def test_report_separates_task_state_from_notification_state(
    client: TestClient, store: Store
) -> None:
    post(client, "issues", load_fixture("issue_labeled.json"), "d-4")
    report = build_report(store)

    assert report["totals"]["tasks"] == 1
    assert report["runs_by_state"] == {State.QUEUED.value: 1}
    assert "issue-pipeline report" in render_text(report)
    assert client.get("/report").status_code == 200
    assert client.get("/report.txt").status_code == 200


def test_illegal_transitions_are_refused() -> None:
    check_transition(State.QUEUED, State.STARTING)
    check_transition(State.MERGED, State.MERGED)
    with pytest.raises(IllegalTransitionError):
        check_transition(State.MERGED, State.RUNNING)
    with pytest.raises(IllegalTransitionError):
        check_transition(State.AWAITING_APPROVAL, State.PR_OPEN)
    assert is_terminal(State.NO_OUTPUT)
    assert not is_terminal(State.PR_OPEN)
