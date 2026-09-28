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
"""The live client's requests, checked against the v3 wire format.

The shapes asserted here come from the published v3 OpenAPI document; the
simulated client never exercises them, so this is where the contract with the
provider is pinned.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.devin_client import DevinError, LiveDevinClient
from app.prompts import STRUCTURED_OUTPUT_SCHEMA

ORG = "org-test"
BASE = f"https://api.example/v3/organizations/{ORG}"


def _client(handler: httpx.MockTransport) -> LiveDevinClient:
    return LiveDevinClient(
        base_url="https://api.example",
        org_id=ORG,
        token="cog_test",
        transport=handler,
    )


def test_create_sends_the_fields_the_run_depends_on() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "session_id": "devin-1",
                "url": "https://app/sessions/devin-1",
                "status": "new",
                "tags": ["run:r1"],
                "pull_requests": [],
                "acus_consumed": 0,
            },
        )

    snapshot = _client(httpx.MockTransport(handler)).create_session(
        prompt="fix it",
        title="repo#1: title",
        repo_url="https://github.com/o/r",
        tags=["run:r1"],
        max_acu_limit=7,
        secret_ids=[],
    )

    assert seen["url"] == f"{BASE}/sessions"
    assert seen["auth"] == "Bearer cog_test"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["repos"] == ["https://github.com/o/r"]
    assert body["max_acu_limit"] == 7
    assert body["secret_ids"] == []
    assert body["structured_output_schema"] == STRUCTURED_OUTPUT_SCHEMA
    assert body["structured_output_required"] is True
    assert "playbook_id" not in body
    assert snapshot.session_id == "devin-1"


def test_find_by_tag_reads_the_paginated_items_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get_list("tags") == ["run:r1"]
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "session_id": "devin-other",
                        "url": "u",
                        "status": "running",
                        "tags": ["run:r9"],
                        "pull_requests": [],
                        "acus_consumed": 1,
                    },
                    {
                        "session_id": "devin-1",
                        "url": "u",
                        "status": "running",
                        "status_detail": "working",
                        "tags": ["run:r1", "repo:o/r"],
                        "pull_requests": [{"pr_url": "https://g/o/r/pull/3"}],
                        "acus_consumed": 2.5,
                    },
                ],
                "has_next_page": False,
            },
        )

    found = _client(httpx.MockTransport(handler)).find_session_by_tag("run:r1")

    assert found is not None
    assert found.session_id == "devin-1"
    assert found.pull_requests[0].url == "https://g/o/r/pull/3"


def test_authorization_failures_are_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"title": "Forbidden", "status": 403})

    with pytest.raises(DevinError) as excinfo:
        _client(httpx.MockTransport(handler)).get_session("devin-1")
    assert excinfo.value.retryable is False
