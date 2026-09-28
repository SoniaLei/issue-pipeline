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
"""PR-lifecycle messages thread under, and react to, the run's "PR opened"
post (D-036); the webhook transport keeps working without either."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import ConfigError, Settings
from app.devin_client import SimulatedDevinClient
from app.intake import Intake
from app.slack_client import BotSlackTransport, FakeSlackTransport
from app.states import State
from app.store import Store
from app.worker import Worker
from tests.conftest import deliver, load_script
from tests.test_dashboard import run_to_pr, send


def _kinds(store: Store) -> list[str]:
    return [str(row["kind"]) for row in store.all_notifications()]


def _by_kind(store: Store, kind: str) -> Any:
    rows = [row for row in store.all_notifications() if row["kind"] == kind]
    assert len(rows) == 1, f"expected one {kind}, got {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------- bot transport


def test_pr_opened_is_the_anchor_and_merged_threads_under_it(
    intake: Intake, worker: Worker, store: Store, slack: FakeSlackTransport
) -> None:
    run_id = run_to_pr(intake, worker, store)
    worker.drain_outbox()
    anchor = _by_kind(store, "pr_opened")
    assert anchor["state"] == "sent"
    assert anchor["slack_channel"] == "https://slack.invalid/engineering"
    assert anchor["slack_ts"] is not None
    assert anchor["thread_ts"] is None

    send(intake, "pull_request", "pr_merged.json", run_id)
    assert worker.drain_outbox() == 1

    merged = _by_kind(store, "pr_merged")
    assert merged["thread_ts"] == anchor["slack_ts"]
    assert merged["reaction"] == "white_check_mark"
    assert merged["reaction_error"] is None
    assert slack.thread_of == [None, anchor["slack_ts"]]
    assert slack.reactions == [
        (anchor["slack_channel"], anchor["slack_ts"], "white_check_mark")
    ]


def test_messages_before_the_pr_exists_stay_top_level(
    intake: Intake, worker: Worker, store: Store, slack: FakeSlackTransport
) -> None:
    result = deliver(intake, "issues", "issue_labeled.json")
    for _ in range(3):
        worker.tick()
    deliver(intake, "issues", "issue_unlabeled.json")
    worker.drain_outbox()

    assert "needs_human" in _kinds(store)
    assert all(ts is None for ts in slack.thread_of)
    assert slack.reactions == []
    run = store.get_run(str(result.run_id))
    assert run is not None
    assert run["approval_revoked_by"] == "SoniaLei"


def test_failed_checks_post_once_per_head_with_a_cross(
    intake: Intake, worker: Worker, store: Store, slack: FakeSlackTransport
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_failure.json", run_id)
    # A second failing suite on the same head, and a redelivered one, add
    # nothing: the head already failed.
    send(intake, "check_suite", "check_suite_failure.json", run_id)
    send(
        intake,
        "check_suite",
        "check_suite_failure.json",
        run_id,
        check_suite={
            "id": 999,
            "head_sha": "a1b2c3d4",
            "status": "completed",
            "conclusion": "failure",
            "app": {"slug": "other-ci"},
        },
    )
    assert _kinds(store).count("checks_failed") == 1
    worker.drain_outbox()

    failed = _by_kind(store, "checks_failed")
    anchor = _by_kind(store, "pr_opened")
    assert failed["thread_ts"] == anchor["slack_ts"]
    assert failed["reaction"] == "red_circle"
    text = json.loads(str(failed["payload"]))["text"]
    assert text.startswith(":red_circle: Checks failed")
    assert [name for _, _, name in slack.reactions] == ["red_circle"]


def test_review_findings_and_verified_each_react_on_the_anchor(
    intake: Intake, worker: Worker, store: Store, slack: FakeSlackTransport
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "pull_request_review", "devin_review_findings.json", run_id)
    send(intake, "pull_request_review", "devin_review_findings.json", run_id)
    assert _kinds(store).count("review_findings") == 1

    send(intake, "check_suite", "check_suite_success.json", run_id)
    send(intake, "pull_request_review", "devin_review_clean.json", run_id)
    assert _kinds(store) == ["pr_opened", "review_findings", "verified"]
    worker.drain_outbox()

    assert [name for _, _, name in slack.reactions] == ["mag", "large_green_circle"]
    findings = _by_kind(store, "review_findings")
    assert findings["thread_ts"] == _by_kind(store, "pr_opened")["slack_ts"]
    assert "Devin Review found issues" in json.loads(str(findings["payload"]))["text"]


def test_human_approval_threads_with_a_thumbs_up(
    intake: Intake, worker: Worker, store: Store, slack: FakeSlackTransport
) -> None:
    run_id = run_to_pr(intake, worker, store)
    send(intake, "pull_request_review", "pr_review_approved.json", run_id)
    send(intake, "pull_request_review", "pr_review_approved.json", run_id)
    assert _kinds(store).count("human_review") == 1
    worker.drain_outbox()

    review = _by_kind(store, "human_review")
    assert review["reaction"] == "thumbsup"
    assert (
        "by SoniaLei" in json.loads(str(review["payload"]))["blocks"][0]["text"]["text"]
    )


def test_a_follow_up_waits_for_a_retrying_anchor(
    store: Store, settings: Settings
) -> None:
    slack = FakeSlackTransport(fail_times=1)
    devin = SimulatedDevinClient(script=load_script("simulated_session_events.json"))
    worker = Worker(store, settings, devin, slack, owner="test")
    intake = Intake(store, settings)
    run_id = run_to_pr(intake, worker, store)
    send(intake, "pull_request", "pr_merged.json", run_id)

    # The anchor fails once and backs off; the follow-up waits rather than
    # going out top-level ahead of it.
    assert worker.drain_outbox() == 0
    assert _by_kind(store, "pr_opened")["state"] == "pending"
    assert _by_kind(store, "pr_merged")["attempts"] == 0
    with store.transaction() as conn:
        conn.execute("UPDATE outbox SET next_attempt_at = NULL")

    assert worker.drain_outbox() == 2
    anchor = _by_kind(store, "pr_opened")
    merged = _by_kind(store, "pr_merged")
    assert anchor["state"] == merged["state"] == "sent"
    assert merged["thread_ts"] == anchor["slack_ts"]
    assert slack.thread_of[-2:] == [None, anchor["slack_ts"]]


def test_a_follow_up_goes_top_level_when_the_anchor_failed_for_good(
    intake: Intake, worker: Worker, store: Store, slack: FakeSlackTransport
) -> None:
    run_id = run_to_pr(intake, worker, store)
    anchor_id = int(_by_kind(store, "pr_opened")["id"])
    store.mark_notification_failed(anchor_id, "invalid_auth")

    send(intake, "pull_request", "pr_merged.json", run_id)
    assert worker.drain_outbox() == 1
    merged = _by_kind(store, "pr_merged")
    assert merged["state"] == "sent"
    assert merged["thread_ts"] is None
    assert merged["reaction"] is None
    assert slack.reactions == []


def test_a_failed_reaction_is_recorded_and_does_not_fail_the_reply(
    intake: Intake, worker: Worker, store: Store, slack: FakeSlackTransport
) -> None:
    run_id = run_to_pr(intake, worker, store)
    worker.drain_outbox()
    slack.fail_reactions = True

    send(intake, "pull_request", "pr_merged.json", run_id)
    assert worker.drain_outbox() == 1
    merged = _by_kind(store, "pr_merged")
    assert merged["state"] == "sent"
    assert merged["thread_ts"] is not None
    assert merged["reaction"] == "white_check_mark"
    assert merged["reaction_error"] == "missing_scope"
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.MERGED.value


def test_the_anchor_survives_a_restart(tmp_path: Path, settings: Settings) -> None:
    db = str(tmp_path / "pipeline.db")
    first = Store(db)
    slack = FakeSlackTransport()
    devin = SimulatedDevinClient(script=load_script("simulated_session_events.json"))
    worker = Worker(first, settings, devin, slack, owner="first")
    run_id = run_to_pr(Intake(first, settings), worker, first)
    worker.drain_outbox()
    anchor_ts = _by_kind(first, "pr_opened")["slack_ts"]
    first.close()

    second = Store(db)
    send(Intake(second, settings), "pull_request", "pr_merged.json", run_id)
    slack2 = FakeSlackTransport()
    Worker(second, settings, devin, slack2, owner="second").drain_outbox()
    assert slack2.thread_of == [anchor_ts]
    assert _by_kind(second, "pr_merged")["thread_ts"] == anchor_ts
    second.close()


def test_an_old_database_gains_the_anchor_columns(tmp_path: Path) -> None:
    db = str(tmp_path / "old.db")
    store = Store(db)
    with store.transaction() as conn:
        for column in ("slack_channel", "slack_ts", "thread_ts", "reaction"):
            conn.execute(f"ALTER TABLE outbox DROP COLUMN {column}")
    store.close()

    reopened = Store(db)
    columns = {
        str(row["name"]) for row in reopened._conn.execute("PRAGMA table_info(outbox)")
    }
    assert {"slack_channel", "slack_ts", "thread_ts", "reaction"} <= columns
    reopened.close()


# ------------------------------------------------------------ webhook transport


def test_webhook_transport_still_delivers_everything_top_level(
    store: Store, settings: Settings
) -> None:
    slack = FakeSlackTransport(threads=False)
    devin = SimulatedDevinClient(script=load_script("simulated_session_events.json"))
    worker = Worker(store, settings, devin, slack, owner="test")
    intake = Intake(store, settings)
    run_id = run_to_pr(intake, worker, store)
    send(intake, "check_suite", "check_suite_failure.json", run_id)
    send(intake, "pull_request", "pr_merged.json", run_id)
    worker.drain_outbox()

    rows = store.all_notifications()
    assert [row["state"] for row in rows] == ["sent"] * len(rows)
    assert all(row["slack_ts"] is None for row in rows)
    assert all(row["thread_ts"] is None for row in rows)
    assert all(ts is None for ts in slack.thread_of)
    assert slack.reactions == []


def test_bot_transport_needs_a_channel_for_every_destination(
    settings: Settings,
) -> None:
    bot = Settings(
        **{
            **settings.__dict__,
            "slack_transport": "bot",
            "slack_channels": {"engineering-updates": "C0ENG"},
        }
    )
    assert bot.slack_target_for("engineering-updates") == "C0ENG"
    with pytest.raises(ConfigError):
        bot.slack_target_for("automation-alerts")
    assert settings.slack_target_for("automation-alerts") == (
        "https://slack.invalid/alerts"
    )


# ------------------------------------------------------- Slack Web API client


def _api(
    responder: Any,
) -> tuple[BotSlackTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result = responder(request)
        assert isinstance(result, httpx.Response)
        return result

    return (
        BotSlackTransport("xoxb-test", transport=httpx.MockTransport(handler)),
        seen,
    )


def test_post_message_returns_the_timestamp_and_threads() -> None:
    client, seen = _api(
        lambda request: httpx.Response(
            200, json={"ok": True, "channel": "C0ENG", "ts": "1712.0001"}
        )
    )
    result = client.send("C0ENG", {"text": "hi"}, thread_ts="1700.0009")
    assert result.ok
    assert result.channel == "C0ENG"
    assert result.ts == "1712.0001"
    body = json.loads(seen[0].content)
    assert body == {"channel": "C0ENG", "text": "hi", "thread_ts": "1700.0009"}
    assert seen[0].url.path == "/api/chat.postMessage"
    assert seen[0].headers["authorization"] == "Bearer xoxb-test"


def test_rate_limits_honour_retry_after_and_5xx_retries() -> None:
    client, _ = _api(lambda request: httpx.Response(429, headers={"Retry-After": "7"}))
    result = client.send("C0ENG", {"text": "hi"})
    assert not result.ok
    assert result.retryable
    assert result.retry_after == 7.0

    client, _ = _api(lambda request: httpx.Response(503, text="down"))
    result = client.send("C0ENG", {"text": "hi"})
    assert not result.ok
    assert result.retryable


@pytest.mark.parametrize(
    "error", ["invalid_auth", "channel_not_found", "not_in_channel"]
)
def test_permanent_api_errors_are_not_retried(error: str) -> None:
    client, _ = _api(
        lambda request: httpx.Response(200, json={"ok": False, "error": error})
    )
    result = client.send("C0ENG", {"text": "hi"})
    assert not result.ok
    assert not result.retryable
    assert result.detail == error


def test_already_reacted_counts_as_done() -> None:
    client, seen = _api(
        lambda request: httpx.Response(
            200, json={"ok": False, "error": "already_reacted"}
        )
    )
    result = client.react("C0ENG", "1712.0001", "white_check_mark")
    assert result.ok
    assert result.detail == "already_reacted"
    assert json.loads(seen[0].content) == {
        "channel": "C0ENG",
        "timestamp": "1712.0001",
        "name": "white_check_mark",
    }
    assert seen[0].url.path == "/api/reactions.add"


# ------------------------------------------------------------- re-approval


def test_relabel_by_a_maintainer_reinstates_approval_without_a_second_run(
    intake: Intake, worker: Worker, store: Store
) -> None:
    result = deliver(intake, "issues", "issue_labeled.json")
    run_id = str(result.run_id)
    for _ in range(3):
        worker.tick()
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.RUNNING.value
    session_id = run["session_id"]

    deliver(intake, "issues", "issue_unlabeled.json")
    again = deliver(intake, "issues", "issue_labeled.json")
    assert again.accepted
    assert again.reason == "approval reinstated"
    assert again.run_id == run_id

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == State.RUNNING.value
    assert run["session_id"] == session_id
    assert run["approval_revoked_by"] is None
    assert run["approval_revoked_at"] is None
    assert run["approved_by"] == "SoniaLei"
    assert len(store.runs_for_task(int(run["task_id"]))) == 1
    events = [(e["kind"], e["reason"]) for e in store.events_for_run(run_id)]
    assert ("approval", "reinstated") in events

    # Revoking a second time is news again, not a suppressed duplicate.
    deliver(intake, "issues", "issue_unlabeled.json")
    assert _kinds(store).count("needs_human") == 2
