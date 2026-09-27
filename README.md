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
| 9. Dashboard: workload, results, speed, throughput, health, run timeline | done |

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
`pull_request.opened` → `check_suite` (queued, then passed) →
`pull_request_review` → `pull_request.closed(merged)` and prints the task
state, the Slack messages that would have been sent and the dashboard's
summary. No network. Add `--serve` to keep the store alive and browse
`/dashboard` over that simulated run afterwards.

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

### Running it live

Nothing in the repository decides whether a run is real; the environment does.
Going live is four things, and the service refuses to start in live mode until
the ones it can check are present:

1. **A reachable endpoint.** GitHub must be able to POST to
   `/webhooks/github`. For a durable deployment that is `docker compose up`
   behind any HTTPS host with a persistent volume. To prove the path from a
   laptop or a throwaway VM without hosting anything:

   ```bash
   scripts/run_live.sh --tunnel
   ```

   starts the API and the worker against one store plus a Cloudflare quick
   tunnel, and prints the public webhook and dashboard URLs. The endpoint
   lives as long as the shell does.

2. **A repository webhook** (or a GitHub App) on each repository in
   `REPO_ALLOWLIST`, pointed at that URL, content type `application/json`,
   secret equal to `GITHUB_WEBHOOK_SECRET`, subscribed to *Issues*,
   *Pull requests*, *Pull request reviews* and *Check suites*. The service
   never calls the GitHub API, so the webhook needs no token; it only needs to
   be delivered.

3. **A Devin service user**, org-scoped, holding `UseDevinSessions` (create)
   and `ViewOrgSessions` (get, list). Set `DEVIN_MODE=live`, `DEVIN_ORG_ID`
   and `DEVIN_API_TOKEN`. `DEVIN_MODE=live` is also what makes runs land on
   the dashboard's *Live* view rather than *Simulation*; the two are never
   summed.

4. **A Slack incoming webhook** per logical destination in
   `SLACK_DESTINATIONS`, with `SLACK_MODE=live`.

Then a maintainer in `MAINTAINER_ALLOWLIST` applies `devin-ready` to an issue,
and `/dashboard?env=live` shows the run from `queued` onwards: the session
link once Devin accepts it, the PR once GitHub reports it, checks as suites
complete, the Slack post state, and the worker and delivery heartbeats under
*Integration health*.

## Dashboard

`GET /dashboard` is a self-contained page over the store; `GET /api/dashboard`
is the same data as JSON and `GET /api/tasks/{id}/timeline` is the drawer that
opens when a row is clicked.

**Live and Simulation are separate pages, never a total.** Every run is stored
with the `env` it was created under (`DEVIN_MODE`), and the dashboard reports
exactly one env at a time — `?env=live` or `?env=sim`, defaulting to the
service's own mode. A banner names which one is shown; there is no "all" view,
so a simulated merge can never inflate a live success number.

The outcome words are used precisely, and the page states its definitions:

| Word | Means | Evidence |
| --- | --- | --- |
| PR opened | a pull request exists for the run; checks may be pending | `pull_request.opened` correlated to the run |
| Verified | every check suite GitHub reported for the PR's **current** head completed successfully | `check_suite` events for `checks_head_sha == head_sha`; a new commit resets it |
| Merged | GitHub reported the PR closed with `merged=true` | `pull_request.closed` |
| Blocked / failed | the run needs a human, with the reason and the next action shown | run state + `failure_reason` |

A session saying it added and ran a test is displayed as *the session's
account* and is never counted as verification. "Verified" is currently "all
suites GitHub reported", not "the suites branch protection requires"; reading
branch protection to narrow it is a later uplift.

Sections: **current workload** (queued, running, PR opened, awaiting review,
blocked, failed), **results** (opened / verified / merged), **speed** (median and
p90 from maintainer approval to first verification, with the sample count),
**throughput** (verified and merged per UTC day, last 14 days), **needs
attention** (blocker, age, next action), **integration health** (last GitHub
delivery, last Devin poll, worker heartbeat, Slack failures) and the **task
table** — issue, state, elapsed, tests, PR, Slack status, last update. The
header shows `data as of` (the newest write in the store) separately from when
the page was rendered.

The run timeline is durable: every state transition, session observation, PR
event, check suite, verification, review and Slack attempt is a row in
`run_events`/`outbox`, so the drawer shows what happened, not a reconstruction.

It only knows about runs *this service* tracked. Sessions started by the Devin
Automations are not in its store, so until the service is hosted the Live page
is empty by construction.

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
