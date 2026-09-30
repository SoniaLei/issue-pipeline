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
"""Slack transports.

Two live transports behind one interface. The destination is always chosen
by a configured key and is not derivable from anything a stranger can write
into an issue; what the key resolves to differs:

* ``webhook`` — an incoming-webhook URL. The channel is a property of the URL.
  The response carries no message timestamp, so every message is top-level.
* ``bot`` — a channel ID posted to with a bot token via ``chat.postMessage``.
  The response carries ``ts``, which is what threading (``thread_ts``) and
  reactions (``reactions.add``) need.

A transport reports whether it can thread so the caller can fall back to a
top-level message rather than lose a notification.

The bot can also look a message up in channel history (``conversations.history``,
scopes ``channels:history`` / ``groups:history``). That is how a run whose
"PR opened" post went out through a webhook, and so has no stored ``ts``, still
gets a thread (D-042).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

SLACK_API_BASE = "https://slack.com/api"

# Slack error codes that a later attempt may not repeat. Everything else from
# the Web API (bad channel, missing scope, bad token, not in channel) fails
# identically forever.
_RETRYABLE_API_ERRORS = frozenset(
    {"ratelimited", "rate_limited", "internal_error", "service_unavailable"}
)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    detail: str
    retryable: bool = False
    retry_after: float | None = None
    # Identity of the posted message when the transport reports one.
    channel: str | None = None
    ts: str | None = None


class SlackTransport(Protocol):
    threads: bool

    def send(
        self, target: str, payload: dict[str, Any], thread_ts: str | None = None
    ) -> SendResult: ...

    def react(self, channel: str, ts: str, name: str) -> SendResult: ...

    def find_message(
        self, channel: str, *, text: str, contains: str, oldest: float, latest: float
    ) -> SendResult: ...


def matches_message(message: dict[str, Any], *, text: str, contains: str) -> bool:
    """A top-level message whose text is ``text`` and whose body mentions
    ``contains`` somewhere (the run id, which the title alone does not pin)."""
    thread_ts = message.get("thread_ts")
    if thread_ts and thread_ts != message.get("ts"):
        return False
    if message.get("text") != text:
        return False
    return contains in json.dumps(message.get("blocks") or [], ensure_ascii=False)


class LiveSlackTransport:
    """Incoming webhooks."""

    threads = False

    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.Client(timeout=timeout)

    def send(
        self, target: str, payload: dict[str, Any], thread_ts: str | None = None
    ) -> SendResult:
        # thread_ts is ignored: a webhook has nothing to thread under.
        try:
            response = self._client.post(target, json=payload)
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

    def react(self, channel: str, ts: str, name: str) -> SendResult:
        return SendResult(False, "incoming webhooks cannot add reactions")

    def find_message(
        self, channel: str, *, text: str, contains: str, oldest: float, latest: float
    ) -> SendResult:
        return SendResult(False, "incoming webhooks cannot read history")


class BotSlackTransport:
    """Web API with a bot token (scopes: ``chat:write``, ``reactions:write``;
    ``channels:history`` / ``groups:history`` for :meth:`find_message`)."""

    threads = True

    def __init__(
        self,
        token: str,
        timeout: float = 10.0,
        api_base: str = SLACK_API_BASE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    def send(
        self, target: str, payload: dict[str, Any], thread_ts: str | None = None
    ) -> SendResult:
        body: dict[str, Any] = {"channel": target, **payload}
        if thread_ts:
            body["thread_ts"] = thread_ts
        return self._call("chat.postMessage", body)

    def react(self, channel: str, ts: str, name: str) -> SendResult:
        result = self._call(
            "reactions.add", {"channel": channel, "timestamp": ts, "name": name}
        )
        if not result.ok and result.detail == "already_reacted":
            # Reactions are idempotent from the reader's side, so a repeat
            # attempt after a crash between the reply and the reaction is fine.
            return SendResult(True, "already_reacted", channel=channel, ts=ts)
        return result

    def find_message(
        self, channel: str, *, text: str, contains: str, oldest: float, latest: float
    ) -> SendResult:
        """The ``ts`` of the first top-level message in ``[oldest, latest]``
        that :func:`matches_message` accepts."""
        params = {
            "channel": channel,
            "oldest": f"{oldest:.6f}",
            "latest": f"{latest:.6f}",
            "inclusive": "true",
            "limit": "200",
        }
        messages: list[dict[str, Any]] = []
        result = self._request(
            "GET", "conversations.history", params=params, messages=messages
        )
        if not result.ok:
            return result
        for message in messages:
            if matches_message(message, text=text, contains=contains):
                return SendResult(
                    True, "found", channel=channel, ts=str(message.get("ts"))
                )
        return SendResult(False, "not_found")

    def _call(self, method: str, body: dict[str, Any]) -> SendResult:
        return self._request("POST", method, body=body)

    def _request(
        self,
        verb: str,
        method: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        try:
            response = self._client.request(
                verb, f"{self._api_base}/{method}", json=body, params=params
            )
        except httpx.TimeoutException:
            return SendResult(False, "timeout", retryable=True)
        except httpx.HTTPError as exc:
            return SendResult(False, f"transport error: {exc}", retryable=True)

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "30"))
            return SendResult(
                False, "rate limited", retryable=True, retry_after=retry_after
            )
        if response.status_code >= 500:
            return SendResult(
                False, f"slack returned {response.status_code}", retryable=True
            )
        try:
            data = response.json()
        except ValueError:
            return SendResult(
                False, f"slack returned {response.status_code}: {response.text[:200]}"
            )
        if not isinstance(data, dict):  # pragma: no cover - defensive
            return SendResult(False, "slack returned a non-object body")
        if data.get("ok") is True:
            if messages is not None:
                messages.extend(
                    m for m in data.get("messages") or [] if isinstance(m, dict)
                )
            channel = data.get("channel")
            ts = data.get("ts")
            return SendResult(
                True,
                "ok",
                channel=str(channel) if channel else None,
                ts=str(ts) if ts else None,
            )
        error = str(data.get("error") or f"http {response.status_code}")
        return SendResult(False, error, retryable=error in _RETRYABLE_API_ERRORS)


@dataclass
class FakeSlackTransport:
    """Records messages instead of sending them.

    Used to get formatting, escaping and the delivery policy right before any
    real channel is involved. Behaves like the bot transport (returns a
    timestamp, accepts reactions) unless ``threads`` is set to False, which
    makes it behave like a webhook.
    """

    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    thread_of: list[str | None] = field(default_factory=list)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    # Channel history `find_message` searches, as (channel, message) pairs.
    history: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    lookups: int = 0
    fail_times: int = 0
    fail_reactions: bool = False
    threads: bool = True

    def send(
        self, target: str, payload: dict[str, Any], thread_ts: str | None = None
    ) -> SendResult:
        if self.fail_times > 0:
            self.fail_times -= 1
            return SendResult(False, "simulated transient failure", retryable=True)
        self.sent.append((target, payload))
        self.thread_of.append(thread_ts if self.threads else None)
        if not self.threads:
            return SendResult(True, "ok")
        ts = f"1700000000.{len(self.sent):06d}"
        return SendResult(True, "ok", channel=target, ts=ts)

    def react(self, channel: str, ts: str, name: str) -> SendResult:
        if not self.threads:
            return SendResult(False, "incoming webhooks cannot add reactions")
        if self.fail_reactions:
            return SendResult(False, "missing_scope")
        self.reactions.append((channel, ts, name))
        return SendResult(True, "ok", channel=channel, ts=ts)

    def find_message(
        self, channel: str, *, text: str, contains: str, oldest: float, latest: float
    ) -> SendResult:
        self.lookups += 1
        if not self.threads:
            return SendResult(False, "incoming webhooks cannot read history")
        for where, message in self.history:
            ts = float(str(message.get("ts") or 0))
            if (
                where == channel
                and oldest <= ts <= latest
                and matches_message(message, text=text, contains=contains)
            ):
                return SendResult(True, "found", channel=channel, ts=str(message["ts"]))
        return SendResult(False, "not_found")

    def texts(self) -> list[str]:
        return [str(payload.get("text", "")) for _, payload in self.sent]
