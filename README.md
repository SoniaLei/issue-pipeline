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
opens or needs attention — with the rest of the PR's life (checks, review,
merge) threaded under that post. Humans keep review and merge.

The design this implements lives in `docs/`: `architecture.md` (components,
state machine, schema), `decisions.md` (the decision record, D-001 onward, with
rationale) and `open-questions.md` (what is still undecided and what the
default is). New here? Start with `docs/onboarding.md`, or with the generated
[DeepWiki](https://deepwiki.com/SoniaLei/issue-pipeline) for a guided tour with
diagrams — see [Documentation and the wiki](#documentation-and-the-wiki) for
which of these is authoritative.

It is a standalone service and shares no code, dependencies or database with
the repositories it watches. It began life under `issue-pipeline/` in
[SoniaLei/superset-cognition-demo](https://github.com/SoniaLei/superset-cognition-demo)
and was split out with its history intact.

## How it relates to the Devin Automations

Devin Automations are configured in Devin, not in either repository, so
nothing here changes them. Two exist for `SoniaLei/superset-cognition-demo`:
the label Automation (a `devin-ready` label → session → PR) is **disabled**,
because it and this service both watched the same label and started duplicate
sessions on the first live run; the merged-PR → Slack Automation is still
enabled. This service is the durable version of the label flow — pre-spend
maintainer gate, webhook dedupe, task state, PR correlation, the review gate —
and picks up triage whenever it is hosted behind a public HTTPS endpoint. A
third Automation, the overnight sweep of *this* repository, is described under
[Documentation and the wiki](#documentation-and-the-wiki).

## What is built

Build-order steps 1–4, which is everything that needs no credential:

| Step | State |
| --- | --- |
| 1. Store, schema, state machine | done |
| 2. Webhook intake, signature verification, dedupe | done |
| 3. Simulated Devin adapter, worker loop | done |
| 4. Outbox, notification formatting, fake Slack transport | done |
| 5. Live Slack transport | done — webhook verified against a real channel; bot transport (threads + reactions, D-036) implemented, awaiting a token |
| 6. Live Devin adapter | implemented, needs a service-user token |
| 7. Reconciliation loop | partial — poll and tag-based orphan recovery |
| 8. Report endpoint | done |
| 9. Dashboard: workload, results, speed, throughput, health, run timeline | done |
| 10. Devin analytics: cost & efficiency, provider cross-check | done |
| 11. Devin Review gate on the PR's current head | done |

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
`pull_request.opened` → `check_suite` (queued, then passed) → a simulated
Devin Review (requested, run, clear verdict) → `pull_request_review` →
`pull_request.closed(merged)` and prints the task
state, the Slack messages that would have been sent and the dashboard's
summary. No network. Add `--serve` to keep the store alive and browse
`/dashboard` over that simulated run afterwards.

To prove the notification path against a real channel without spending an ACU,
keep Devin simulated and send for real:

```bash
export SLACK_WEBHOOK_ENGINEERING_UPDATES='https://hooks.slack.com/services/...'
python scripts/run_simulation.py --live-slack
```

Or, to see the PR thread and reactions for real, with a bot token instead:

```bash
export SLACK_BOT_TOKEN='xoxb-...' SLACK_CHANNEL_ENGINEERING_UPDATES='C0...'
python scripts/run_simulation.py --live-slack-bot
```

Messages arrive in the channel behind that URL or id, so only run it against a
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
   *Pull requests*, *Pull request reviews*, *Pull request review comments*
   and *Check suites*. Review comments carry Devin Review's inline findings
   (the per-kind counts on the dashboard); without them only the review's
   total arrives. The service never calls the GitHub API, so the webhook
   needs no token; it only needs to be delivered.

3. **A Devin service user**, org-scoped, holding `UseDevinSessions` (create)
   and `ViewOrgSessions` (get, list); the same token is used for the
   `pr-reviews` and analytics endpoints. Set `DEVIN_MODE=live`, `DEVIN_ORG_ID`
   and `DEVIN_API_TOKEN`. `DEVIN_MODE=live` is also what makes runs land on
   the dashboard's *Live* view rather than *Simulation*; the two are never
   summed.

4. **Slack**, with `SLACK_MODE=live`, in one of two transports:
   - `SLACK_TRANSPORT=webhook` (default): an incoming webhook per logical
     destination in `SLACK_DESTINATIONS`. Every message is top-level.
   - `SLACK_TRANSPORT=bot`: a bot token in `SLACK_BOT_TOKEN` (scopes
     `chat:write`, `reactions:write`; create at https://api.slack.com/apps →
     *OAuth & Permissions* → install → *Bot User OAuth Token*) and a channel
     id per destination in `SLACK_CHANNELS`
     (`engineering-updates=C0…,automation-alerts=C0…`; the id is at the
     bottom of a channel's *About* tab). Invite the bot to each channel
     (`/invite @app`). The run's *PR opened* post becomes its anchor: checks
     failed, Devin Review findings, verified, human review, merged and
     closed arrive as replies in its thread, each adding a reaction to the
     anchor (:red_circle: :mag: :large_green_circle: :thumbsup:
     :white_check_mark: :no_entry_sign:),
     so the channel reads one line per PR. Follow-ups wait for a retrying
     anchor and fall back to top-level if it never delivered; a reaction
     that fails is shown in the run's timeline, never retried, and never
     touches the run (D-036).

Then a maintainer in `MAINTAINER_ALLOWLIST` applies `devin-ready` to an issue,
and `/dashboard?env=live` shows the run from `queued` onwards: the session
link once Devin accepts it, the PR once GitHub reports it, checks as suites
complete, the Slack post state, and the worker and delivery heartbeats under
*Integration health*.

**Dashboard access.** `/dashboard`, `/api/*` and `/report*` show issue
numbers, approver logins, session URLs and spend, and are served from the same
origin as the public webhook. When `DASHBOARD_TOKEN` is set, every path except
`/webhooks/github` (which is HMAC-signed) and `/health` needs it. Browsers show
their HTTP Basic prompt (any username, the token as the password), and scripts
send `Authorization: Bearer <token>`. Live mode refuses to start without it,
and `scripts/run_live.sh --tunnel` refuses too. In sim mode, leaving it unset
keeps the dashboard open and logs a warning. `compose.yaml` and
`scripts/run_live.sh` listen on `127.0.0.1` by default, so an untokened
dashboard is reachable only from that machine. Set `BIND_ADDRESS` (for
example `0.0.0.0` behind an HTTPS proxy on another host) to expose it;
`run_live.sh` refuses a non-loopback address without `DASHBOARD_TOKEN`.

## Dashboard

`GET /dashboard` is a self-contained page over the store; `GET /api/dashboard`
is the same data as JSON and `GET /api/tasks/{id}/timeline` is the drawer that
opens when a row is clicked.

**Live and Simulation are separate pages, never a total.** Every run is stored
with the `env` it was created under (`DEVIN_MODE`), and the dashboard reports
exactly one env at a time — `?env=live` or `?env=sim`, defaulting to the
service's own mode. A banner names which one is shown; there is no "all" view,
so a simulated merge can never inflate a live success number.

**One repository or all of them.** The service tracks every repository in
`REPO_ALLOWLIST`, and `?repo=owner/name` (the repository picker in the header)
narrows workload, results, speed, throughput, needs attention, Slack health,
cost and the task table to that repository. Without it the page covers every
tracked repository in the chosen env. The Devin analytics cross-check always
spans all repositories, because Devin counts per service user rather than per
repository; `devin_analytics.scope` says so. A repository with no runs gives an
empty page rather than an error.

The outcome words are used precisely, and the page states its definitions:

| Word | Means | Evidence |
| --- | --- | --- |
| PR opened | a pull request exists for the run; checks may be pending | `pull_request.opened` correlated to the run |
| Checks passed | every check suite GitHub reported for the PR's **current** head completed successfully | `check_suite` events for `checks_head_sha == head_sha`; a new commit resets it |
| Review clear | Devin Review of that **same** head reported no findings on GitHub | the Devin bot's `pull_request_review` for `head_sha`; a new commit resets it |
| Verified | checks passed and, when the review gate is `required`, review clear — both on the current head | the two rows above |
| Merged | GitHub reported the PR closed with `merged=true` | `pull_request.closed` |
| Blocked / failed | the run needs a human, with the reason and the next action shown | run state + `failure_reason` |

A session saying it added and ran a test is displayed as *the session's
account* and is never counted as verification. "Checks passed" is currently
"all suites GitHub reported", not "the suites branch protection requires";
reading branch protection to narrow it is a later uplift. Human review is
shown as its own fact and is required for merge regardless of any of the above.

Sections: **current workload** (queued, running, PR opened, awaiting review,
blocked, failed), **results** (opened / verified / merged), **speed** (median and
p90 from maintainer approval to first verification, with the sample count),
**throughput** (verified and merged per UTC day, last 14 days by default;
`?days=N` or the header picker sets 1 to 90), **needs
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

### Devin Review gate

After a PR opens, the worker asks Devin Review to look at the PR's current
head — once per commit — and reads its progress from the organization
`pr-reviews` API (`pending`, `running`, `completed`, `errored`, `cancelled`,
`skipped`). What the review *found* does not come from that API: it is the
review the Devin bot posts on GitHub for that exact commit ("found N potential
issues" / "no issues found", with inline comments carrying a kind such as
`bug` or `security`). Both are stored on one row per `(run, head)` in
`pr_reviews`, with a durable `review_gate` event for every request, status
change, verdict, error and superseded head.

`REVIEW_GATE_MODE` decides what the review means for *verified*:

| Mode | Review calls | Verified means |
| --- | --- | --- |
| `required` (default) | yes | checks passed **and** review clear on the current head |
| `advisory` | yes, shown everywhere | checks passed (review displayed, not required) |
| `off` | none | checks passed |

A `completed` status with no GitHub verdict is *awaiting verdict*, not clear.
Findings do not fail the run: it stays PR-open and unverified with the count
and kinds in the task row and timeline, and what to do about them — fix,
dismiss on GitHub with a reason, or raise a follow-up issue — is the
reviewer's call. A new commit keeps the old head's review for audit and starts
a fresh request; nothing from an earlier head counts. Provider errors are
stored on that head, retried up to `REVIEW_MAX_ATTEMPTS` times
`REVIEW_POLL_SECONDS` apart on idle worker ticks (a permanent error such as
a missing permission is not retried at all), then shown as *unavailable*
under Needs attention; they never stop the worker or touch another run.

The mode is read at two different moments, and changing it mid-history shows
the difference. The *verified* count and each task row are recomputed from
stored checks and reviews under the mode in force **now**. Speed samples and
daily throughput come from the durable `verified` event, which is written once
when a head first satisfied the gate under the mode in force **then** (the
event records that mode). Tightening the mode leaves earlier samples in place;
loosening it does not backfill samples for heads that would now count. Pick
the mode per environment before it carries data, and treat a change as a new
series.

The gate is a second reader, not a second approver: a PR that is checks-green
and review-clear still needs a maintainer to approve and merge it. See D-033.

### Devin analytics: cost, efficiency and a cross-check

Beside GitHub's facts the dashboard shows what Devin says about the same runs,
read by the worker from the organization analytics endpoints and stored per
environment (`session_insights`, `provider_metrics`). Everything from this
source is labelled **Devin analytics** / **provider** on the page; none of it
changes a run's state or outcome.

**Cost & efficiency** — total ACUs, ACUs per PR opened / verified / merged,
median and p90 per run, spend on terminal runs that produced no PR, the
session-size distribution (XS–XL, with the L+XL share as the provider's own
"this ran long" signal), categories, and the action items and skill usage from
Devin's session analysis. Each ACU figure names its source: `billing` (daily
consumption, billing-grade, published at Pacific midnight and often hours
late), `session` (the insights total) or `poll` (the last snapshot the worker
saw). A session the provider has not priced yet is counted under *awaiting
cost* and shown as *pending* — never as zero, and never in a ratio: ACUs per PR
is `—` until at least one session is priced. Sample counts sit next to every
ratio.

**Provider cross-check** in Integration health — Devin's `metrics/sessions` and
`metrics/prs` counts for the pipeline's service user, over a window covering
every run, beside the pipeline's own counts from GitHub. Equal counts read
`ok`; any difference reads `drift` with the per-line delta. GitHub stays
authoritative: a gap is reported, not reconciled. The counts are only requested
once a session has reported which service user it ran as, so a human's
sessions in the same org are never counted as pipeline work.

**Devin's account** in the timeline drawer — per run: ACUs and source, daily
consumption, size, category, message counts, analysis status, and the analysis
itself (issues, action items by type, good and bad skill uses, suggested
prompt). Analysis is requested once for finished sessions the provider did not
analyse on its own.

Analytics never compete with runs: the worker makes one analytics read (a
single session, or the org counts) only on a tick that had no run to advance,
with a shorter per-request timeout than session calls. It re-reads a run when
its state or session status moves and otherwise every
`INSIGHTS_REFRESH_SECONDS`, and keeps reading a terminal run for
`INSIGHTS_SETTLE_SECONDS` so late billing and analysis land — after that the
run is left as it stands, analysed or not. A failed read is stored as
`last_error` beside the last good figures and their window, and shown on the
page. `ANALYTICS_ENABLED=false` switches all of it off.

## Configuration

Everything is environment configuration; nothing is derived from issue or PR
content. See `.env.example` for the full list.

The two that decide whether anything happens at all:

- `REPO_ALLOWLIST` — `owner/name` pairs the pipeline will act on. An event for
  any other repository is acknowledged and dropped.
- `MAINTAINER_ALLOWLIST` — GitHub logins whose `devin-ready` label is treated
  as authorization to spend. Nobody else's is.

Review gate: `REVIEW_GATE_MODE` (`required` by default; `advisory`, `off`),
`REVIEW_POLL_SECONDS` (60), `REVIEW_MAX_ATTEMPTS` (5).

Analytics: `ANALYTICS_ENABLED` (default `true`), `INSIGHTS_REFRESH_SECONDS`
(600), `INSIGHTS_SETTLE_SECONDS` (86400), `METRICS_REFRESH_SECONDS` (300). The
same `DEVIN_API_TOKEN` is used; the organization-level analytics endpoints
answered to the service user's existing `UseDevinSessions` + `ViewOrgSessions`
when probed, and the `/v3/enterprise` variants (which need
`ViewAccountMetrics`) are not used.

## Testing

```bash
pytest
ruff check . && ruff format --check . && mypy app scripts
```

The suite runs against fixtures and the simulated adapters. No test touches a
network. `pre-commit install` runs the same checks on each commit; CI runs
them plus the simulated end-to-end run on every push and pull request.

## Documentation and the wiki

Five kinds of text describe this system. They answer different questions and
only the first three are authoritative:

| Read | For | Changed by |
| --- | --- | --- |
| this README | how to run, configure and read it | reviewed PR |
| `docs/architecture.md` | what it is meant to do and where the boundaries are | reviewed PR |
| `docs/decisions.md` | why, what each choice is *not*, and when to revisit it | reviewed PR |
| `.agents/skills/` in a **target** repository (e.g. [superset-cognition-demo](https://github.com/SoniaLei/superset-cognition-demo/tree/master/.agents/skills)) | how a Devin session stands up and tests *that* repository (D-031) | reviewed PR there |
| [DeepWiki](https://deepwiki.com/SoniaLei/issue-pipeline) | a generated tour of the code as it is — diagrams, per-area pages, source links | regenerated from the code |

**DeepWiki** is Devin's generated wiki for a connected repository. It is the
fastest way in for a new engineer and the context Ask Devin and Devin sessions
read, and it is steered by `.devin/wiki.json` at the repository root, which
names the pages it should have (overview, lifecycle, review gate, security,
operations, onboarding …) and carries the notes that tell the generator what
this system is and which documents are canonical. Change the page tree by
changing that file in a PR. What the wiki cannot hold is intent that the code
does not yet implement, a rejected alternative or a "revisit when" — those are
in `docs/decisions.md`, so when the wiki and the docs disagree, the docs say
what was meant, the wiki says what was built, and the gap is a bug in one of
them. Reading the wiki, or asking Devin about the repository, creates nothing:
no issue, no PR, no session (D-034).

**Ask Devin** — in the Devin app, or on the public wiki page — answers
questions about this repository grounded in the wiki and code ("where is the
maintainer gate enforced?", "what resets verification?"). Agents can read the
same material through the DeepWiki MCP (`read_wiki_structure`,
`read_wiki_contents`, `ask_question`).

**Overnight sweep.** One scheduled Devin Automation runs nightly against this
repository: it compares the code with the README, `architecture.md` and
`decisions.md`, looks for defects it can demonstrate with a test, and for each
actionable finding opens a GitHub issue (needs a decision, or not small) or a
PR with a test (small and clearly right), then posts one Slack summary. It is
bounded — fixed prompt, one run per night, no overlap, an ACU cap — and it
never merges: its PRs go through CI, the review gate and human review like any
other, and its issues go through the `devin-ready` gate before any further
spend (D-035).

A short reading path for new engineers is in `docs/onboarding.md`.
