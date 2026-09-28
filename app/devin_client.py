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

The analytics surface (Session Insights, daily consumption, organization PR
and session metrics) is read through the same client. What it returns is
the provider's account of its own work — cost, size, what went wrong in its
view — and is stored and shown as such, never as the outcome of a run.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from app.prompts import STRUCTURED_OUTPUT_SCHEMA
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


@dataclass(frozen=True)
class SessionInsights:
    """Devin's own analysis of one session.

    ``analysis`` is the provider's AI-generated review (issues, action items,
    skill usage, suggested prompt), present only once the provider has run it;
    ``analysis_status`` says where that stands. Everything is kept verbatim.
    """

    session_id: str
    status: str
    status_detail: str | None = None
    acus_consumed: float | None = None
    session_size: str | None = None
    category: str | None = None
    subcategory: str | None = None
    origin: str | None = None
    service_user_id: str | None = None
    num_user_messages: int | None = None
    num_devin_messages: int | None = None
    analysis_status: str | None = None
    analysis: dict[str, Any] | None = None


@dataclass(frozen=True)
class DailyConsumption:
    """Billing-grade ACU usage for one session, one row per billing day.

    Billing days start at midnight Pacific (08:00 UTC); ``by_date`` keeps the
    provider's day boundary as an ISO timestamp rather than re-bucketing it.
    """

    total_acus: float
    by_date: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class ProviderMetrics:
    """Organization-level counts for one window and one set of identities.

    These count what Devin saw its sessions do, including sessions this
    service never tracked. They exist to be compared with the store, not to
    replace it.
    """

    prs_created: int
    prs_opened: int
    prs_merged: int
    prs_closed: int
    sessions_created: int
    sessions_with_merged_prs: int
    avg_acus_per_session: float | None
    sessions_by_size: dict[str, int] = field(default_factory=dict)


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

    def get_session_insights(self, session_id: str) -> SessionInsights: ...

    def request_session_insights(self, session_id: str) -> str: ...

    def get_session_consumption(self, session_id: str) -> DailyConsumption: ...

    def get_provider_metrics(
        self,
        *,
        service_user_ids: list[str],
        time_after: datetime,
        time_before: datetime,
    ) -> ProviderMetrics: ...


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


def _parse_insights(data: dict[str, Any]) -> SessionInsights:
    def _int(value: Any) -> int | None:
        return int(value) if value is not None else None

    return SessionInsights(
        session_id=str(data.get("session_id") or data.get("devin_id") or ""),
        status=str(data.get("status") or "new"),
        status_detail=data.get("status_detail"),
        acus_consumed=data.get("acus_consumed"),
        session_size=data.get("session_size"),
        category=data.get("category"),
        subcategory=data.get("subcategory"),
        origin=data.get("origin"),
        service_user_id=data.get("service_user_id"),
        num_user_messages=_int(data.get("num_user_messages")),
        num_devin_messages=_int(data.get("num_devin_messages")),
        analysis_status=data.get("analysis_status"),
        analysis=data.get("analysis"),
    )


def _parse_consumption(data: dict[str, Any]) -> DailyConsumption:
    rows = []
    for row in data.get("consumption_by_date") or []:
        day = row.get("date")
        if isinstance(day, (int, float)):
            day = datetime.fromtimestamp(day, tz=timezone.utc).isoformat()
        rows.append((str(day), float(row.get("acus") or 0.0)))
    return DailyConsumption(
        total_acus=float(data.get("total_acus") or 0.0), by_date=tuple(rows)
    )


def _parse_metrics(prs: dict[str, Any], sessions: dict[str, Any]) -> ProviderMetrics:
    avg = sessions.get("avg_acus_per_session")
    return ProviderMetrics(
        prs_created=int(prs.get("prs_created_count") or 0),
        prs_opened=int(prs.get("prs_opened_count") or 0),
        prs_merged=int(prs.get("prs_merged_count") or 0),
        prs_closed=int(prs.get("prs_closed_count") or 0),
        sessions_created=int(sessions.get("sessions_created_count") or 0),
        sessions_with_merged_prs=int(
            sessions.get("sessions_with_merged_prs_count") or 0
        ),
        avg_acus_per_session=float(avg) if avg is not None else None,
        sessions_by_size={
            str(k): int(v)
            for k, v in (sessions.get("sessions_created_by_size") or {}).items()
        },
    )


class LiveDevinClient:
    """The v3 organization API.

    Endpoints, all under ``/v3/organizations/{org_id}``:

    ==================  ==========================================
    create              ``POST   /sessions``
    get                 ``GET    /sessions/{devin_id}``
    list                ``GET    /sessions``
    message             ``POST   /sessions/{devin_id}/messages``
    insights            ``GET    /sessions/{devin_id}/insights``
    generate insights   ``POST   /sessions/{devin_id}/insights/generate``
    consumption         ``GET    /consumption/daily/sessions/{id}``
    PR metrics          ``GET    /metrics/prs``
    session metrics     ``GET    /metrics/sessions``
    ==================  ==========================================

    Authentication is a dedicated service user holding ``UseDevinSessions``
    (create) and ``ViewOrgSessions`` (get, list), not an individual's token: an
    unattended pipeline keyed to one person's account stops working the moment
    their access changes. The organization-level analytics endpoints answer to
    the same token; the ``/v3/enterprise`` variants need ``ViewAccountMetrics``
    and are not used.
    """

    def __init__(
        self,
        *,
        base_url: str,
        org_id: str,
        token: str,
        timeout: float = 30.0,
        analytics_timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = f"{base_url.rstrip('/')}/v3/organizations/{org_id}"
        # Analytics reads share the worker thread with run processing, so
        # they get a short leash: a slow insights endpoint costs a tick, not
        # a run.
        self._analytics_timeout = analytics_timeout
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
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
            # The session must report its outcome in this shape before it ends;
            # it is what the dashboard shows as test evidence.
            "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
            "structured_output_required": True,
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
        data = self._request("GET", "/sessions", params={"tags": [tag]})
        for item in data.get("items") or []:
            snapshot = _parse_session(item)
            if tag in snapshot.tags:
                return snapshot
        return None

    def send_message(self, session_id: str, message: str) -> None:
        self._request(
            "POST", f"/sessions/{session_id}/messages", json={"message": message}
        )

    def _analytics(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._request(method, path, timeout=self._analytics_timeout, **kwargs)

    def get_session_insights(self, session_id: str) -> SessionInsights:
        return _parse_insights(
            self._analytics("GET", f"/sessions/{session_id}/insights")
        )

    def request_session_insights(self, session_id: str) -> str:
        data = self._analytics("POST", f"/sessions/{session_id}/insights/generate")
        return str(data.get("status") or "requested")

    def get_session_consumption(self, session_id: str) -> DailyConsumption:
        return _parse_consumption(
            self._analytics("GET", f"/consumption/daily/sessions/{session_id}")
        )

    def get_provider_metrics(
        self,
        *,
        service_user_ids: list[str],
        time_after: datetime,
        time_before: datetime,
    ) -> ProviderMetrics:
        params: dict[str, Any] = {
            "service_user_ids": service_user_ids,
            "time_after": int(time_after.timestamp()),
            "time_before": int(time_before.timestamp()),
        }
        return _parse_metrics(
            self._analytics("GET", "/metrics/prs", params=params),
            self._analytics("GET", "/metrics/sessions", params=params),
        )


SIM_SERVICE_USER = "service-user-sim"


@dataclass
class _SimSession:
    session_id: str
    url: str
    tags: list[str]
    script: list[dict[str, Any]]
    position: int = 0
    messages: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    insights_requested: bool = False


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
        return self._snapshot(self._get(session_id), advance=True)

    def find_session_by_tag(self, tag: str) -> SessionSnapshot | None:
        for session in self._sessions.values():
            if tag in session.tags:
                return self._snapshot(session, advance=False)
        return None

    def send_message(self, session_id: str, message: str) -> None:
        self._sessions[session_id].messages.append(message)

    def get_session_insights(self, session_id: str) -> SessionInsights:
        session = self._get(session_id)
        step = session.script[session.position]
        finished = step["status"] == "exit" or step.get("status_detail") == "finished"
        given = step.get("insights") or {}
        # As with the provider: analysis exists only for a completed session.
        analysis = given.get("analysis") if finished else None
        return SessionInsights(
            session_id=session.session_id,
            status=step["status"],
            status_detail=step.get("status_detail"),
            acus_consumed=step.get("acus_consumed"),
            session_size=given.get("session_size", "s"),
            category=given.get("category", "bug_fixing"),
            subcategory=given.get("subcategory"),
            origin="api",
            service_user_id=SIM_SERVICE_USER,
            num_user_messages=1 + len(session.messages),
            num_devin_messages=session.position + 1,
            analysis_status="completed" if analysis is not None else None,
            analysis=analysis,
        )

    def request_session_insights(self, session_id: str) -> str:
        session = self._get(session_id)
        status = "already_exists" if session.insights_requested else "started"
        session.insights_requested = True
        return status

    def get_session_consumption(self, session_id: str) -> DailyConsumption:
        session = self._get(session_id)
        acus = float(session.script[session.position].get("acus_consumed") or 0.0)
        if acus == 0.0:
            return DailyConsumption(total_acus=0.0)
        day = session.created_at.replace(hour=8, minute=0, second=0, microsecond=0)
        return DailyConsumption(total_acus=acus, by_date=((day.isoformat(), acus),))

    def get_provider_metrics(
        self,
        *,
        service_user_ids: list[str],
        time_after: datetime,
        time_before: datetime,
    ) -> ProviderMetrics:
        if SIM_SERVICE_USER not in service_user_ids:
            return ProviderMetrics(0, 0, 0, 0, 0, 0, None)
        sessions = [
            s
            for s in self._sessions.values()
            if time_after <= s.created_at <= time_before
        ]
        prs_by_session = {
            s.session_id: [
                pr
                for pr in s.script[s.position].get("pull_requests") or []
                if pr.get("pr_url")
            ]
            for s in sessions
        }
        prs = [pr for group in prs_by_session.values() for pr in group]
        acus = [
            float(s.script[s.position].get("acus_consumed") or 0.0) for s in sessions
        ]
        return ProviderMetrics(
            prs_created=len(prs),
            prs_opened=len([pr for pr in prs if pr.get("pr_state") == "open"]),
            prs_merged=len([pr for pr in prs if pr.get("pr_state") == "merged"]),
            prs_closed=len([pr for pr in prs if pr.get("pr_state") == "closed"]),
            sessions_created=len(sessions),
            sessions_with_merged_prs=sum(
                1
                for group in prs_by_session.values()
                if any(pr.get("pr_state") == "merged" for pr in group)
            ),
            avg_acus_per_session=(sum(acus) / len(acus)) if acus else None,
            sessions_by_size={"s": len(sessions)} if sessions else {},
        )

    def _get(self, session_id: str) -> _SimSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise DevinError(f"unknown session {session_id}", retryable=False) from exc

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
