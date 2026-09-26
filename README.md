<!--
Licensed to the Apache Software Foundation (ASF) under one or more
contributor license agreements.  See the NOTICE file distributed with
this work for additional information regarding copyright ownership.
The ASF licenses this file to You under the Apache License, Version 2.0
(the "License"); you may not use this file except in compliance with
the License.  You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# issue-pipeline

An event-driven service that tracks GitHub issues, delegates approved
engineering work to Devin, and notifies a Slack channel when a pull request
opens or needs attention. Humans keep review and merge.

The design this implements lives in `docs/`: `architecture.md` (components,
state machine, schema), `decisions.md` (29 decisions with rationale) and
`open-questions.md` (what is still undecided and what the default is).

It is deliberately self-contained under `issue-pipeline/` and shares no code,
dependencies or database with Superset itself.

## What is built

Build-order steps 1–4, which is everything that needs no credential:

| Step | State |
| --- | --- |
| 1. Store, schema, state machine | done |
| 2. Webhook intake, signature verification, dedupe | done |
| 3. Simulated Devin adapter, worker loop | done |
| 4. Outbox, notification formatting, fake Slack transport | done |
| 5. Live Slack transport | implemented, needs an authorized webhook URL |
| 6. Live Devin adapter | implemented, needs a service-user token |
| 7. Reconciliation loop | partial — poll and tag-based orphan recovery |
| 8. Report endpoint | done |

The whole pipeline runs end to end with `DEVIN_MODE=sim` and
`SLACK_MODE=fake`, spending nothing and calling nobody.

## Running it

```bash
cd issue-pipeline
python3 -m venv .venv && . .venv/bin/activate   # or your usual venv
pip install -r requirements.txt

cp .env.example .env          # edit: at minimum GITHUB_WEBHOOK_SECRET
export $(grep -v '^#' .env | xargs)

uvicorn app.main:get_app --factory --port 8000   # API
python -m app.worker                             # worker, separate shell
```

Or `docker compose up --build`, which runs both against a shared volume.

### A full simulated run

```bash
python scripts/run_simulation.py
```

Replays `issue_opened` → `issue_labeled` → a simulated Devin session →
`pull_request.opened` → `pull_request.closed(merged)` and prints the task
state and the Slack messages that would have been sent. No network.

### Replaying a captured delivery

```bash
python scripts/replay_webhook.py fixtures/issue_labeled.json
```

Signs the body with `GITHUB_WEBHOOK_SECRET` and posts it to a running API,
which is also the easiest way to check signature verification is on.

## Configuration

Everything is environment configuration; nothing is derived from issue or PR
content. See `.env.example` for the full list.

The two that decide whether anything happens at all:

- `REPO_ALLOWLIST` — `owner/name` pairs the pipeline will act on. An event for
  any other repository is acknowledged and dropped.
- `MAINTAINER_ALLOWLIST` — GitHub logins whose `devin-ready` label is treated
  as authorization to spend. Nobody else's is.

## Testing

```bash
cd issue-pipeline && pytest tests
```

The suite runs against fixtures and the simulated adapters. No test touches a
network.
