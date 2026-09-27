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

It is a standalone service and shares no code, dependencies or database with
the repositories it watches. It began life under `issue-pipeline/` in
[SoniaLei/superset-cognition-demo](https://github.com/SoniaLei/superset-cognition-demo)
and was split out with its history intact.

## How it relates to the Devin Automations

Two Devin Automations act on `SoniaLei/superset-cognition-demo` today without
this service running anywhere: one turns a `devin-ready` label into a Devin
session that opens a PR, the other posts to Slack when a `devin/issue-*` PR is
merged. They are configured in Devin, not in either repository, so moving this
code does not affect them. This service is the durable version of that flow —
pre-spend maintainer gate, webhook dedupe, task state, PR correlation — for
when it is hosted behind a public HTTPS endpoint.

## What is built

Build-order steps 1–4, which is everything that needs no credential:

| Step | State |
| --- | --- |
| 1. Store, schema, state machine | done |
| 2. Webhook intake, signature verification, dedupe | done |
| 3. Simulated Devin adapter, worker loop | done |
| 4. Outbox, notification formatting, fake Slack transport | done |
| 5. Live Slack transport | done — verified against a real channel |
| 6. Live Devin adapter | implemented, needs a service-user token |
| 7. Reconciliation loop | partial — poll and tag-based orphan recovery |
| 8. Report endpoint | done |

The whole pipeline runs end to end with `DEVIN_MODE=sim` and
`SLACK_MODE=fake`, spending nothing and calling nobody.

## Running it

```bash
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

To prove the notification path against a real channel without spending an ACU,
keep Devin simulated and send for real:

```bash
export SLACK_WEBHOOK_ENGINEERING_UPDATES='https://hooks.slack.com/services/...'
python scripts/run_simulation.py --live-slack
```

Two messages arrive in the channel behind that URL, so only run it against a
destination whose owner has authorized it.

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
pytest
ruff check . && ruff format --check . && mypy app scripts
```

The suite runs against fixtures and the simulated adapters. No test touches a
network. `pre-commit install` runs the same checks on each commit; CI runs
them plus the simulated end-to-end run on every push and pull request.

## Observability

Open `http://localhost:8000/dashboard` after starting the API. Select **sim**
(the default with simulated Devin) or **live**, optionally filter by repository,
and choose 1–90 UTC calendar days for throughput. `/reports/summary` accepts the
same `env`, `repo` and `days` parameters and returns JSON. `/runs/{run_id}` shows
recorded state transitions and notification attempts.

The dashboard separates current workload from observed PR/merge throughput,
shows unsuccessful and blocked attempts, and preserves unknown ACU usage as
unknown. Median approval-to-PR time includes only runs with recorded approval
and PR transitions and shows the sample count. These are elapsed times, not
engineer time saved. Current state counts and timing samples cover all matching
runs; only throughput is restricted to the selected window.

State history is persisted transactionally through SQLite triggers. Existing
runs acquire a labelled snapshot on upgrade: earlier milestone times cannot be
reconstructed and do not contribute to throughput. Back up the database before
upgrading. No existing run or notification rows are rewritten. Throughput counts
observed state transitions, not the original remote event timestamps.

CI evaluation remains outside this worker (D-011). The dashboard explicitly
shows checks as **not evaluated** rather than treating an opened or merged PR
as proof of a passing regression test. Native Devin Automations that run without
this service do not automatically populate its database. The dashboard only
reports work recorded by this service.

Notification state is independent of remediation state. The mode describes the
Devin run: simulated runs can still send real Slack messages when explicitly
configured. Report generation and provider poll times are not worker-health
checks. The existing `/report` and `/report.txt` remain legacy aggregate views;
use the new dashboard/summary for isolated modes and unknown-usage accounting.

These read-only routes follow the existing report's access model: they are not
authenticated. Keep them private or behind authenticated ingress. Expose only
`/webhooks/github` through a public webhook tunnel. Raw webhook payloads, session
outputs and Slack destination URLs are not included in the new report.
