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
"""Task state machine.

The unhappy paths are named states rather than an absence of progress: a
session can block on a human, expire, or finish without producing a pull
request, and a design whose only exit from `running` is "a PR appeared" leaves
those tasks alive forever with nobody told.
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    """Application state of a task. Distinct from the provider's own status."""

    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    SESSION_BLOCKED = "session_blocked"
    PR_OPEN = "pr_open"
    # Defined for check-based review readiness, unreachable while D-011 keeps
    # check evaluation out of v1.
    AWAITING_REVIEW = "awaiting_review"
    NO_OUTPUT = "no_output"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MERGED = "merged"
    CLOSED_UNMERGED = "closed_unmerged"


TERMINAL: frozenset[State] = frozenset(
    {
        State.MERGED,
        State.CLOSED_UNMERGED,
        State.CANCELLED,
        State.FAILED,
        State.NO_OUTPUT,
        State.EXPIRED,
    }
)

# States in which a run holds the one active slot for a repository/issue. The
# partial unique index in the schema is defined over exactly this set, so
# historical attempts do not collide with a new one.
ACTIVE: frozenset[State] = frozenset(
    {
        State.AWAITING_APPROVAL,
        State.QUEUED,
        State.STARTING,
        State.RUNNING,
        State.SESSION_BLOCKED,
        State.PR_OPEN,
        State.AWAITING_REVIEW,
    }
)

# A run in one of these has not yet started spending, so revoking approval
# cancels it outright (D-006). Once it is past here the session is left to
# finish: the spend is sunk either way and stopping only discards the work.
PRE_EXECUTION: frozenset[State] = frozenset({State.AWAITING_APPROVAL, State.QUEUED})

TRANSITIONS: dict[State, frozenset[State]] = {
    State.AWAITING_APPROVAL: frozenset({State.QUEUED, State.CANCELLED}),
    State.QUEUED: frozenset({State.STARTING, State.CANCELLED}),
    State.STARTING: frozenset({State.RUNNING, State.FAILED, State.PR_OPEN}),
    State.RUNNING: frozenset(
        {
            State.SESSION_BLOCKED,
            State.PR_OPEN,
            State.NO_OUTPUT,
            State.EXPIRED,
            State.FAILED,
        }
    ),
    State.SESSION_BLOCKED: frozenset(
        {State.RUNNING, State.PR_OPEN, State.NO_OUTPUT, State.EXPIRED, State.FAILED}
    ),
    State.PR_OPEN: frozenset(
        {State.AWAITING_REVIEW, State.MERGED, State.CLOSED_UNMERGED}
    ),
    State.AWAITING_REVIEW: frozenset(
        {State.PR_OPEN, State.MERGED, State.CLOSED_UNMERGED}
    ),
    State.NO_OUTPUT: frozenset(),
    State.EXPIRED: frozenset(),
    State.FAILED: frozenset(),
    State.CANCELLED: frozenset(),
    State.MERGED: frozenset(),
    State.CLOSED_UNMERGED: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    """Raised when a transition is not permitted by the state machine."""

    def __init__(self, current: State, target: State) -> None:
        super().__init__(f"cannot move from {current.value} to {target.value}")
        self.current = current
        self.target = target


def can_transition(current: State, target: State) -> bool:
    return target in TRANSITIONS[current]


def check_transition(current: State, target: State) -> None:
    """Raise unless the transition is legal.

    A no-op transition is legal and silent: webhook redelivery is normal, and
    the second delivery of an event that has already been applied must not be
    an error.
    """
    if current is target:
        return
    if not can_transition(current, target):
        raise IllegalTransitionError(current, target)


def is_terminal(state: State) -> bool:
    return state in TERMINAL
