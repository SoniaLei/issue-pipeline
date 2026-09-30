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
"""Read-only GitHub REST client for reconciliation (D-041).

Webhooks tell the service what happened; this client lets it ask what *is*.
Every method is a GET. The objects come back as the REST API returns them,
which for issues, pull requests, check suites and reviews is the same shape
the corresponding webhook carries, so the reconciler can hand them to the
intake as if they had been delivered. Only the fields the intake reads are
relied upon: ``number``, ``state``, ``title``, ``labels``, ``updated_at`` on
issues; ``number``, ``html_url``, ``state``, ``draft``, ``merged``,
``merge_commit_sha``, ``merged_by``, ``body``, ``head.{sha,ref,repo}`` on
pulls; ``id``, ``head_sha``, ``status``, ``conclusion``, ``app``, ``url`` on
check suites; ``id``, ``user``, ``state``, ``commit_id``, ``body``,
``html_url`` on reviews; ``protection.required_status_checks`` on branches,
which GitHub includes only for callers with push access and which is read as
an observation, not a gate (D-041).

The token is a bearer token with read access to Issues, Pull requests, Checks
and Metadata on the allowlisted repositories — an installation token of the
App in D-027, or a fine-grained token scoped the same way. It has no merge
permission and this module has no method that could use one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

USER_AGENT = "issue-pipeline-reconciler"
PAGE_SIZE = 100
MAX_PAGES = 10


class GitHubError(RuntimeError):
    """A GitHub read failed in a way the caller must decide about."""

    def __init__(
        self, message: str, *, retryable: bool = True, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class GitHubClient(Protocol):
    """What reconciliation needs from GitHub, and nothing else."""

    def get_issue(self, repo: str, number: int) -> dict[str, Any]: ...

    def list_issue_events(self, repo: str, number: int) -> list[dict[str, Any]]: ...

    def get_pull(self, repo: str, number: int) -> dict[str, Any]: ...

    def list_pulls_for_branch(self, repo: str, branch: str) -> list[dict[str, Any]]: ...

    def get_branch(self, repo: str, branch: str) -> dict[str, Any]: ...

    def list_check_suites(self, repo: str, sha: str) -> list[dict[str, Any]]: ...

    def list_reviews(self, repo: str, number: int) -> list[dict[str, Any]]: ...


_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


def next_page(link_header: str | None) -> str | None:
    """The ``rel="next"`` URL of a ``Link`` header, if there is one."""
    if not link_header:
        return None
    match = _NEXT_LINK.search(link_header)
    return match.group(1) if match else None


class LiveGitHubClient:
    """The REST API, read-only.

    ==================  ==================================================
    issue               ``GET /repos/{repo}/issues/{n}``
    issue events        ``GET /repos/{repo}/issues/{n}/events``
    pull                ``GET /repos/{repo}/pulls/{n}``
    pulls by branch     ``GET /repos/{repo}/pulls?head={owner}:{branch}&state=all``
    branch              ``GET /repos/{repo}/branches/{name}``
    check suites        ``GET /repos/{repo}/commits/{sha}/check-suites``
    reviews             ``GET /repos/{repo}/pulls/{n}/reviews``
    ==================  ==================================================

    Reads share the worker thread with run processing, so the timeout is
    short: a slow GitHub costs one reconciliation pass, not a run.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            },
        )

    def _get(self, url: str, **params: Any) -> httpx.Response:
        if url.startswith("/"):
            url = f"{self._base}{url}"
        try:
            response = self._client.get(url, params=params or None)
        except httpx.TimeoutException as exc:
            raise GitHubError(f"timeout reading {url}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise GitHubError(str(exc), retryable=True) from exc
        code = response.status_code
        if code in {401, 403}:
            raise GitHubError(
                f"not authorized for {url} ({code})", retryable=False, status_code=code
            )
        if code >= 500 or code == 429:
            raise GitHubError(
                f"{url} returned {code}", retryable=True, status_code=code
            )
        if code >= 400:
            raise GitHubError(
                f"{url} returned {code}: {response.text[:200]}",
                retryable=False,
                status_code=code,
            )
        return response

    def _object(self, path: str) -> dict[str, Any]:
        data: dict[str, Any] = self._get(path).json()
        return data

    def _list(
        self, path: str, key: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url: str | None = path
        pages = 0
        while url and pages < MAX_PAGES:
            response = self._get(
                url, **({"per_page": PAGE_SIZE, **params} if pages == 0 else {})
            )
            data = response.json()
            page = data[key] if key else data
            items.extend(page)
            url = next_page(response.headers.get("link"))
            pages += 1
        return items

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return self._object(f"/repos/{repo}/issues/{number}")

    def list_issue_events(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._list(f"/repos/{repo}/issues/{number}/events")

    def get_pull(self, repo: str, number: int) -> dict[str, Any]:
        return self._object(f"/repos/{repo}/pulls/{number}")

    def list_pulls_for_branch(self, repo: str, branch: str) -> list[dict[str, Any]]:
        owner = repo.partition("/")[0]
        return self._list(f"/repos/{repo}/pulls", head=f"{owner}:{branch}", state="all")

    def get_branch(self, repo: str, branch: str) -> dict[str, Any]:
        return self._object(f"/repos/{repo}/branches/{branch}")

    def list_check_suites(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return self._list(
            f"/repos/{repo}/commits/{sha}/check-suites", key="check_suites"
        )

    def list_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._list(f"/repos/{repo}/pulls/{number}/reviews")


def _not_found(what: str) -> GitHubError:
    return GitHubError(f"{what} not found", retryable=False, status_code=404)


@dataclass
class SimulatedGitHubClient:
    """GitHub as a dictionary. Tests and the simulation set what GitHub
    currently says; the reconciler reads it exactly as it would read the API."""

    issues: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    issue_events: dict[tuple[str, int], list[dict[str, Any]]] = field(
        default_factory=dict
    )
    pulls: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    check_suites: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict
    )
    reviews: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    branches: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def set_issue(
        self,
        repo: str,
        issue: dict[str, Any],
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.issues[(repo.lower(), int(issue["number"]))] = issue
        if events is not None:
            self.issue_events[(repo.lower(), int(issue["number"]))] = events

    def set_pull(
        self,
        repo: str,
        pull: dict[str, Any],
        *,
        check_suites: list[dict[str, Any]] | None = None,
        reviews: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pulls[(repo.lower(), int(pull["number"]))] = pull
        sha = str((pull.get("head") or {}).get("sha") or "")
        if check_suites is not None and sha:
            self.check_suites[(repo.lower(), sha)] = check_suites
        if reviews is not None:
            self.reviews[(repo.lower(), int(pull["number"]))] = reviews

    def set_branch(
        self, repo: str, name: str, *, required_checks: list[str] | None = None
    ) -> None:
        """What GitHub would say about a branch; ``required_checks=None`` is an
        unprotected branch, ``[]`` protection without required checks."""
        branch: dict[str, Any] = {
            "name": name,
            "protected": required_checks is not None,
        }
        if required_checks is not None:
            branch["protection"] = {
                "enabled": True,
                "required_status_checks": {
                    "enforcement_level": "non_admins",
                    "contexts": list(required_checks),
                    "checks": [{"context": c, "app_id": None} for c in required_checks],
                },
            }
        self.branches[(repo.lower(), name)] = branch

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        self.calls.append(("issue", repo, str(number)))
        try:
            return self.issues[(repo.lower(), number)]
        except KeyError:
            raise _not_found(f"issue {repo}#{number}") from None

    def list_issue_events(self, repo: str, number: int) -> list[dict[str, Any]]:
        self.calls.append(("issue_events", repo, str(number)))
        return list(self.issue_events.get((repo.lower(), number), []))

    def get_pull(self, repo: str, number: int) -> dict[str, Any]:
        self.calls.append(("pull", repo, str(number)))
        try:
            return self.pulls[(repo.lower(), number)]
        except KeyError:
            raise _not_found(f"pull {repo}#{number}") from None

    def list_pulls_for_branch(self, repo: str, branch: str) -> list[dict[str, Any]]:
        self.calls.append(("pulls_for_branch", repo, branch))
        return [
            pull
            for (pull_repo, _), pull in self.pulls.items()
            if pull_repo == repo.lower()
            and str((pull.get("head") or {}).get("ref") or "") == branch
        ]

    def get_branch(self, repo: str, branch: str) -> dict[str, Any]:
        self.calls.append(("branch", repo, branch))
        try:
            return self.branches[(repo.lower(), branch)]
        except KeyError:
            raise _not_found(f"branch {repo}@{branch}") from None

    def list_check_suites(self, repo: str, sha: str) -> list[dict[str, Any]]:
        self.calls.append(("check_suites", repo, sha))
        return list(self.check_suites.get((repo.lower(), sha), []))

    def list_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        self.calls.append(("reviews", repo, str(number)))
        return list(self.reviews.get((repo.lower(), number), []))
