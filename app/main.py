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
"""HTTP surface: the GitHub webhook, health, and the task report.

The handler does verification, deduplication and a transaction, and nothing
else. Session creation and Slack delivery belong to the worker, because a
webhook that waits on a third party is a webhook GitHub gives up on.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import load_settings, Settings
from app.intake import Intake, verify_signature
from app.reporting import build_report, render_text
from app.store import Store

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    resolved = settings or load_settings()
    backing = store or Store(resolved.database_path)
    intake = Intake(backing, resolved)

    app = FastAPI(title="issue-pipeline", version="0.1.0")
    app.state.settings = resolved
    app.state.store = backing
    app.state.intake = intake

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "env": resolved.env,
            "devin_mode": resolved.devin_mode,
            "slack_mode": resolved.slack_mode,
        }

    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
    ) -> Response:
        body = await request.body()
        if not verify_signature(
            resolved.github_webhook_secret, body, x_hub_signature_256
        ):
            return JSONResponse({"detail": "invalid signature"}, status_code=401)
        if not x_github_delivery or not x_github_event:
            return JSONResponse({"detail": "missing headers"}, status_code=400)
        try:
            payload: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"detail": "invalid JSON"}, status_code=400)

        result = intake.handle(
            delivery_id=x_github_delivery, event=x_github_event, payload=payload
        )
        # Anything that got this far is acknowledged. A 4xx to GitHub for a
        # payload we simply do not act on buys redeliveries of an event that
        # will be ignored identically each time.
        return JSONResponse(
            {
                "accepted": result.accepted,
                "reason": result.reason,
                "task_id": result.task_id,
                "run_id": result.run_id,
            }
        )

    @app.get("/report")
    def report() -> dict[str, Any]:
        return build_report(backing)

    @app.get("/report.txt", response_class=PlainTextResponse)
    def report_text() -> str:
        return render_text(build_report(backing))

    return app


def get_app() -> FastAPI:  # pragma: no cover - uvicorn entry point
    """Entry point for `uvicorn app.main:get_app --factory`.

    A factory rather than a module-level instance: configuration is validated
    when the process starts, so a missing webhook secret is a startup failure
    rather than a service that accepts unsigned deliveries.
    """
    logging.basicConfig(level=logging.INFO)
    return create_app()
