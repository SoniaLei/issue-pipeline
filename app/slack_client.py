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
"""Slack transport.

Incoming webhooks only, for now. The channel is a property of the URL and a
payload cannot override it, which is exactly the property wanted here: the
destination is chosen by a configured key and is not derivable from anything a
stranger can write into an issue.

The cost of that choice is that the response carries no message timestamp, so
threading and message edits need a Web API bot adapter behind this same
interface later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class SendResult:
    ok: bool
    detail: str
    retryable: bool = False
    retry_after: float | None = None


class SlackTransport(Protocol):
    def send(self, webhook_url: str, payload: dict[str, Any]) -> SendResult: ...


class LiveSlackTransport:
    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.Client(timeout=timeout)

    def send(self, webhook_url: str, payload: dict[str, Any]) -> SendResult:
        try:
            response = self._client.post(webhook_url, json=payload)
        except httpx.TimeoutException:
            # Slack may have accepted it. Retrying can duplicate the message;
            # delivery here is at-least-once and cannot be made exactly-once.
            return SendResult(False, "timeout", retryable=True)
        except httpx.HTTPError as exc:
            return SendResult(False, f"transport error: {exc}", retryable=True)

        if response.status_code == 200:
            return SendResult(True, response.text[:200])
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "30"))
            return SendResult(
                False, "rate limited", retryable=True, retry_after=retry_after
            )
        if response.status_code >= 500:
            return SendResult(
                False, f"slack returned {response.status_code}", retryable=True
            )
        # 400s from an incoming webhook are configuration or payload problems
        # and will fail identically forever.
        return SendResult(
            False, f"slack returned {response.status_code}: {response.text[:200]}"
        )


@dataclass
class FakeSlackTransport:
    """Records messages instead of sending them.

    Used to get formatting, escaping and the delivery policy right before any
    real channel is involved.
    """

    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_times: int = 0

    def send(self, webhook_url: str, payload: dict[str, Any]) -> SendResult:
        if self.fail_times > 0:
            self.fail_times -= 1
            return SendResult(False, "simulated transient failure", retryable=True)
        self.sent.append((webhook_url, payload))
        return SendResult(True, "ok")

    def texts(self) -> list[str]:
        return [str(payload.get("text", "")) for _, payload in self.sent]
