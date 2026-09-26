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
"""Drive the whole pipeline end to end against simulated integrations.

No network, no credentials, no ACUs. Prints the resulting task report and the
Slack messages that would have been sent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.devin_client import SimulatedDevinClient  # noqa: E402
from app.intake import Intake  # noqa: E402
from app.reporting import build_report, render_text  # noqa: E402
from app.slack_client import FakeSlackTransport  # noqa: E402
from app.store import Store  # noqa: E402
from app.worker import Worker  # noqa: E402

FIXTURES = ROOT / "fixtures"
REPO = "SoniaLei/superset-cognition-demo"


def fixture(name: str) -> dict[str, Any]:
    with open(FIXTURES / name, encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> None:
    settings = Settings(
        github_webhook_secret="simulation",
        repo_allowlist=frozenset({REPO.lower()}),
        maintainer_allowlist=frozenset({"sonialei"}),
        database_path=":memory:",
        slack_destinations={
            "engineering-updates": "https://slack.invalid/engineering",
            "automation-alerts": "https://slack.invalid/alerts",
        },
    )
    store = Store(settings.database_path)
    intake = Intake(store, settings)
    slack = FakeSlackTransport()
    with open(FIXTURES / "simulated_session_events.json", encoding="utf-8") as handle:
        script = json.load(handle)
    worker = Worker(
        store, settings, SimulatedDevinClient(script=script), slack, owner="simulation"
    )

    print("1. issue opened by a stranger -> recorded, not authorized")
    intake.handle(
        delivery_id="sim-1", event="issues", payload=fixture("issue_opened.json")
    )

    print("2. devin-ready applied by someone without authority -> ignored")
    intake.handle(
        delivery_id="sim-2",
        event="issues",
        payload=fixture("issue_labeled_untrusted.json"),
    )

    print("3. devin-ready applied by a maintainer -> run queued")
    result = intake.handle(
        delivery_id="sim-3", event="issues", payload=fixture("issue_labeled.json")
    )
    run_id = str(result.run_id)

    print("4. worker creates a session and polls it to completion")
    for _ in range(5):
        worker.tick()

    print("5. the PR webhook arrives and correlates to the run")
    pr = json.loads(json.dumps(fixture("pr_opened.json")).replace("RUN_ID", run_id))
    intake.handle(delivery_id="sim-4", event="pull_request", payload=pr)
    worker.tick()

    print("6. a human merges it")
    merged = json.loads(json.dumps(fixture("pr_merged.json")).replace("RUN_ID", run_id))
    intake.handle(delivery_id="sim-5", event="pull_request", payload=merged)
    worker.tick()

    print()
    print(render_text(build_report(store)))
    print("Slack messages that would have been sent:")
    for url, payload in slack.sent:
        print(f"  -> {url}")
        print("     " + payload["blocks"][0]["text"]["text"].replace("\n", "\n     "))


if __name__ == "__main__":
    main()
