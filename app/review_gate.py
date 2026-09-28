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
"""The review gate: Devin Review on the PR's current head, as a second
condition for *verified* beside GitHub checks (D-033).

Two sources, kept apart the way checks and structured output are:

* The pr-reviews API says whether a review of a given head was requested,
  is running, completed, errored or was skipped. It is the *progress* of
  the review and the handle used to request one; it says nothing about
  what the review found.
* The verdict comes from GitHub: Devin Review submits a pull request review
  as ``devin-ai-integration[bot]`` whose summary says how many issues it
  found. That review is tied to a commit, arrives through the same signed
  webhook as every other GitHub fact, and is the only thing that can make a
  head *clear*. A completed review with no verdict seen yet is not clear.

Absence is unknown, never a pass: a head with no review row has not been
reviewed, and a review that errored is a review that did not happen.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

MODES = ("off", "advisory", "required")

# The GitHub login Devin Review submits its summary review under.
REVIEW_BOT_LOGINS = frozenset({"devin-ai-integration[bot]"})

# Progress statuses the pr-reviews API reports, plus the one the worker
# records when it could not reach the API at all.
API_TERMINAL = frozenset({"completed", "errored", "cancelled", "skipped"})
API_FAILED = frozenset({"errored", "cancelled", "skipped"})
UNAVAILABLE = "unavailable"

_FOUND = re.compile(r"found\s+(\d+)\s+potential\s+issues?", re.IGNORECASE)
_CLEAN = re.compile(r"no\s+issues?\s+found", re.IGNORECASE)

# Finding ids in Devin Review's comment markers are prefixed with the kind:
# ``BUG_…``, ``SECURITY_…``, ``FLAG_…``.
_MARKER = re.compile(r"<!--\s*devin-review-comment\s*(\{.*?\})\s*-->", re.DOTALL)
_KIND = re.compile(r"^([A-Za-z]+)_")


def is_review_bot(login: str | None) -> bool:
    return bool(login) and str(login).lower() in REVIEW_BOT_LOGINS


def parse_summary(body: str | None) -> int | None:
    """Number of findings a Devin Review summary reports, or None if the
    text does not carry a verdict."""
    if not body:
        return None
    match = _FOUND.search(body)
    if match:
        return int(match.group(1))
    if _CLEAN.search(body):
        return 0
    return None


def finding_kind(comment_body: str | None) -> str | None:
    """The kind of a Devin Review inline comment (``bug``, ``security``,
    ``flag``), read from its marker; None for any other comment."""
    if not comment_body:
        return None
    match = _MARKER.search(comment_body)
    if not match:
        return None
    try:
        marker: dict[str, Any] = json.loads(match.group(1))
    except ValueError:
        return None
    if marker.get("kind"):
        return str(marker["kind"]).lower()
    ident = str(marker.get("id") or "")
    kind = _KIND.match(ident)
    return kind.group(1).lower() if kind else "other"


def gate_state(row: sqlite3.Row | None, mode: str) -> str:
    """One word for where the gate stands for a head.

    ``off`` when the gate is disabled; ``not_requested`` when nothing has
    been asked; ``pending``/``running`` while the API works; ``clear`` or
    ``findings`` once GitHub carries the verdict; ``awaiting_verdict`` for a
    completed review whose GitHub summary has not arrived; ``errored``,
    ``skipped``, ``cancelled`` or ``unavailable`` when the review did not
    happen.
    """
    if mode == "off":
        return "off"
    if row is None:
        return "not_requested"
    findings = row["findings"]
    if findings is not None:
        # GitHub's verdict is the verdict, whatever the API said about progress.
        return "clear" if int(findings) == 0 else "findings"
    status = row["status"]
    if status is None:
        # Seen on GitHub (a bot review without a summary) but not yet asked
        # about; the worker reads the API on its next tick.
        return "not_requested"
    status = str(status)
    if status == "completed":
        return "awaiting_verdict"
    if status in API_FAILED or status == UNAVAILABLE:
        return status
    return status  # pending | running


def gate_satisfied(state: str, mode: str) -> bool:
    """Whether the gate, in this mode, lets a head count as verified."""
    if mode in {"off", "advisory"}:
        return True
    return state == "clear"


def checks_passed(run: sqlite3.Row) -> bool:
    """GitHub checks passed on the head GitHub currently reports for the PR."""
    return bool(
        run["pr_number"] is not None
        and run["head_sha"]
        and run["checks_state"] == "passed"
        and run["checks_head_sha"] == run["head_sha"]
    )


def is_verified(run: sqlite3.Row, review: sqlite3.Row | None, mode: str) -> bool:
    """Checks green on the current head, and the review gate satisfied for
    the same head. Both conditions read GitHub; neither reads Devin's word
    for it."""
    if not checks_passed(run):
        return False
    if review is not None and review["head_sha"] != run["head_sha"]:
        review = None
    return gate_satisfied(gate_state(review, mode), mode)
