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
"""Shared fixtures. Nothing here touches a network."""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.devin_client import SimulatedDevinClient
from app.intake import Intake
from app.slack_client import FakeSlackTransport
from app.store import Store
from app.worker import Worker

REPO = "SoniaLei/superset-cognition-demo"
MAINTAINER = "SoniaLei"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

_delivery_ids = itertools.count(1)


def load_fixture(name: str) -> dict[str, Any]:
    with open(FIXTURES / name, encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


def next_delivery_id() -> str:
    return f"delivery-{next(_delivery_ids):05d}"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        github_webhook_secret="test-secret",
        repo_allowlist=frozenset({REPO.lower()}),
        maintainer_allowlist=frozenset({MAINTAINER.lower()}),
        database_path=":memory:",
        slack_destinations={
            "engineering-updates": "https://slack.invalid/engineering",
            "automation-alerts": "https://slack.invalid/alerts",
        },
    )


@pytest.fixture
def store() -> Iterator[Store]:
    store = Store(":memory:")
    yield store
    store.close()


@pytest.fixture
def intake(store: Store, settings: Settings) -> Intake:
    return Intake(store, settings)


@pytest.fixture
def slack() -> FakeSlackTransport:
    return FakeSlackTransport()


@pytest.fixture
def devin() -> SimulatedDevinClient:
    return SimulatedDevinClient(
        script=load_script("simulated_session_events.json"),
    )


def load_script(name: str) -> list[dict[str, Any]]:
    with open(FIXTURES / name, encoding="utf-8") as handle:
        script: list[dict[str, Any]] = json.load(handle)
    return script


@pytest.fixture
def worker(
    store: Store,
    settings: Settings,
    devin: SimulatedDevinClient,
    slack: FakeSlackTransport,
) -> Worker:
    return Worker(store, settings, devin, slack, owner="test-worker")


def deliver(intake: Intake, event: str, fixture: str, **overrides: Any) -> Any:
    payload = load_fixture(fixture)
    payload.update(overrides)
    return intake.handle(delivery_id=next_delivery_id(), event=event, payload=payload)


def substitute_run_id(payload: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Fixtures carry a RUN_ID placeholder in the branch and body marker."""
    raw = json.dumps(payload).replace("RUN_ID", run_id)
    result: dict[str, Any] = json.loads(raw)
    return result
