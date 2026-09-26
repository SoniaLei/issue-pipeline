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
"""Devin adapter: the v3 organization API, and a simulated stand-in.

Provider vocabulary stops here. Everything above this module speaks the
application's own states, and the mapping between the two is explicit in
`map_status` rather than smeared across the worker.

Two properties of the provider drive the design:

* There is no documented endpoint that stops a running session. `max_acu_limit`
  is set at creation and is the only ceiling that actually holds.
* `finished` is a *detail* under the `running` status, not a status of its own.
  Reading only `status` never observes a session completing.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.states import State


@dataclass(frozen=True)
class PullRequestRef:
    url: str
    state: str | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    """One observation of a session. Provider fields are kept verbatim."""

    session_id: str
    url: str
    status: str
    status_detail: str | None = None
    pull_requests: tuple[PullRequestRef, ...] = ()
    acus_consumed: float | None = None
    structured_output: dict[str, Any] | None = None
    tags: tuple[str, ...] = ()


# Suspension details that mean the organization has a problem, not the task.
CAPACITY_DETAILS = frozenset(
    {
        "usage_limit_exceeded",
        "out_of_credits",
        "out_of_quota",
        "org_usage_limit_exceeded",
        "contract_expired",
    }
)


def map_status(snapshot: SessionSnapshot, has_pr: bool) -> tuple[State, str | None]:
    """Translate provider status into an application state and reason.

    Returns the state the run should be in given this observation. Whether a
    `finished` session with no PR is `no_output` depends on the grace period,
    which is the caller's business; this reports the session's own position.
    """
    status = snapshot.status
    detail = snapshot.status_detail

    if status in {"new", "claimed"}:
        return State.STARTING, None
    if status == "error":
        return State.FAILED, "session_error"
    if status == "resuming":
        return State.RUNNING, None
    if status == "suspended":
        if detail in CAPACITY_DETAILS:
            # Operator problem. Calling this a failed fix sends a maintainer to
            # review a diff that does not exist.
            return State.FAILED, "capacity"
        return State.SESSION_BLOCKED, detail or "suspended"
    if status == "running":
        return _map_running_detail(detail, has_pr)
    if status == "exit":
        return _map_completion(has_pr)
    return State.RUNNING, None


def _map_running_detail(detail: str | None, has_pr: bool) -> tuple[State, str | None]:
    # `finished` lives here, under `running`, rather than being a status of its
    # own: a mapping that reads only `status` never observes completion.
    if detail == "waiting_for_user":
        return State.SESSION_BLOCKED, "waiting_for_user"
    if detail == "waiting_for_approval":
        return State.SESSION_BLOCKED, "approval"
    if detail == "finished":
        return _map_completion(has_pr)
    return State.RUNNING, None


def _map_completion(has_pr: bool) -> tuple[State, str | None]:
    return (State.PR_OPEN, None) if has_pr else (State.RUNNING, "finished_no_pr")


class DevinClient(Protocol):
    """The surface the worker is allowed to use."""

    def create_session(
        self,
        *,
        prompt: str,
        title: str,
        repo_url: str,
        tags: list[str],
        max_acu_limit: int,
        playbook_id: str | None = None,
        secret_ids: list[str] | None = None,
    ) -> SessionSnapshot: ...

    def get_session(self, session_id: str) -> SessionSnapshot: ...

    def find_session_by_tag(self, tag: str) -> SessionSnapshot | None: ...

    def send_message(self, session_id: str, message: str) -> None: ...


class DevinError(RuntimeError):
    """A provider call failed in a way the caller must decide about."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def _parse_session(data: dict[str, Any]) -> SessionSnapshot:
    prs = tuple(
        PullRequestRef(url=pr.get("pr_url", ""), state=pr.get("pr_state"))
        for pr in data.get("pull_requests") or []
        if pr.get("pr_url")
    )
    return SessionSnapshot(
        session_id=str(data.get("session_id") or data.get("devin_id") or ""),
        url=str(data.get("url") or ""),
        status=str(data.get("status") or "new"),
        status_detail=data.get("status_detail"),
        pull_requests=prs,
        acus_consumed=data.get("acus_consumed"),
        structured_output=data.get("structured_output"),
        tags=tuple(data.get("tags") or ()),
    )


class LiveDevinClient:
    """The v3 organization API.

    Endpoints, all under ``/v3/organizations/{org_id}``:

    ==================  ==========================================
    create              ``POST   /sessions``
    get                 ``GET    /sessions/{devin_id}``
    list                ``GET    /sessions``
    message             ``POST   /sessions/{devin_id}/messages``
    ==================  ==========================================

    Authentication is a dedicated service user holding ``UseDevinSessions``,
    not an individual's token: an unattended pipeline keyed to one person's
    account stops working the moment their access changes.
    """

    def __init__(
        self,
        *,
        base_url: str,
        org_id: str,
        token: str,
        timeout: float = 30.0,
    ) -> None:
        self._base = f"{base_url.rstrip('/')}/v3/organizations/{org_id}"
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, f"{self._base}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            # Ambiguous: the call may well have succeeded. The caller must
            # reconcile by tag rather than retry.
            raise DevinError(f"timeout calling {path}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise DevinError(str(exc), retryable=True) from exc

        if response.status_code in {401, 403}:
            raise DevinError(
                f"not authorized for {path} ({response.status_code})", retryable=False
            )
        if response.status_code >= 500 or response.status_code == 429:
            raise DevinError(f"{path} returned {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise DevinError(
                f"{path} returned {response.status_code}: {response.text[:200]}",
                retryable=False,
            )
        parsed: dict[str, Any] = response.json()
        return parsed

    def create_session(
        self,
        *,
        prompt: str,
        title: str,
        repo_url: str,
        tags: list[str],
        max_acu_limit: int,
        playbook_id: str | None = None,
        secret_ids: list[str] | None = None,
    ) -> SessionSnapshot:
        body: dict[str, Any] = {
            "prompt": prompt,
            "title": title,
            "repos": [repo_url],
            "tags": tags,
            # The only spend ceiling that holds, and it cannot be applied later.
            "max_acu_limit": max_acu_limit,
            # Omitting this grants the session every organization secret, so it
            # is always sent, even when empty.
            "secret_ids": secret_ids or [],
        }
        if playbook_id:
            body["playbook_id"] = playbook_id
        return _parse_session(self._request("POST", "/sessions", json=body))

    def get_session(self, session_id: str) -> SessionSnapshot:
        return _parse_session(self._request("GET", f"/sessions/{session_id}"))

    def find_session_by_tag(self, tag: str) -> SessionSnapshot | None:
        """Resolve an ambiguous create by looking for the tag we set on it.

        This is why the run tag is set at creation rather than appended after:
        appending it would leave it absent in exactly the failure case that
        needs it.
        """
        data = self._request("GET", "/sessions", params={"tags": tag})
        sessions = data.get("sessions") or data.get("data") or []
        for item in sessions:
            snapshot = _parse_session(item)
            if tag in snapshot.tags:
                return snapshot
        return None

    def send_message(self, session_id: str, message: str) -> None:
        self._request(
            "POST", f"/sessions/{session_id}/messages", json={"message": message}
        )


@dataclass
class _SimSession:
    session_id: str
    url: str
    tags: list[str]
    script: list[dict[str, Any]]
    position: int = 0
    messages: list[str] = field(default_factory=list)


class SimulatedDevinClient:
    """A scripted stand-in that never leaves the process.

    The whole state machine, including the paths that matter most — blocked,
    finished with no PR, capacity suspension — is exercised against this
    before a single ACU is spent.
    """

    DEFAULT_SCRIPT: list[dict[str, Any]] = [
        {"status": "new"},
        {"status": "running", "status_detail": "working", "acus_consumed": 1.5},
        {
            "status": "running",
            "status_detail": "finished",
            "acus_consumed": 4.0,
            "structured_output": {"outcome": "fixed", "tests_added": True},
            "pull_requests": [{"pr_url": "", "pr_state": "open"}],
        },
    ]

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self._script = script if script is not None else list(self.DEFAULT_SCRIPT)
        self._sessions: dict[str, _SimSession] = {}
        self._counter = itertools.count(1)

    def create_session(
        self,
        *,
        prompt: str,
        title: str,
        repo_url: str,
        tags: list[str],
        max_acu_limit: int,
        playbook_id: str | None = None,
        secret_ids: list[str] | None = None,
    ) -> SessionSnapshot:
        session_id = f"devin-sim-{next(self._counter):04d}"
        session = _SimSession(
            session_id=session_id,
            url=f"https://app.devin.ai/sessions/{session_id}",
            tags=list(tags),
            script=[dict(step) for step in self._script],
        )
        self._sessions[session_id] = session
        return self._snapshot(session, advance=False)

    def get_session(self, session_id: str) -> SessionSnapshot:
        try:
            session = self._sessions[session_id]
        except KeyError as exc:
            raise DevinError(f"unknown session {session_id}", retryable=False) from exc
        return self._snapshot(session, advance=True)

    def find_session_by_tag(self, tag: str) -> SessionSnapshot | None:
        for session in self._sessions.values():
            if tag in session.tags:
                return self._snapshot(session, advance=False)
        return None

    def send_message(self, session_id: str, message: str) -> None:
        self._sessions[session_id].messages.append(message)

    def _snapshot(self, session: _SimSession, *, advance: bool) -> SessionSnapshot:
        if advance and session.position < len(session.script) - 1:
            session.position += 1
        step = session.script[session.position]
        prs = tuple(
            PullRequestRef(url=pr["pr_url"], state=pr.get("pr_state"))
            for pr in step.get("pull_requests") or []
            if pr.get("pr_url")
        )
        return SessionSnapshot(
            session_id=session.session_id,
            url=session.url,
            status=step["status"],
            status_detail=step.get("status_detail"),
            pull_requests=prs,
            acus_consumed=step.get("acus_consumed"),
            structured_output=step.get("structured_output"),
            tags=tuple(session.tags),
        )


def load_script(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        data: list[dict[str, Any]] = json.load(handle)
    return data
