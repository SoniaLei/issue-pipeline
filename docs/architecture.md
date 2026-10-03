# Architecture

An event-driven service that tracks GitHub issues, delegates approved engineering
work to Devin, and notifies a Slack channel as the resulting pull request moves
through review. Engineers retain ownership of review and merge. There is no
automatic merge in the initial release.

## 1. Components

```
                   signed webhooks
   GitHub  ────────────────────────────▶  POST /webhooks/github
     ▲                                          │
     │                                          │ validate, dedupe, persist
     │  REST (issues, PRs, checks, reviews)     ▼
     │                                    ┌───────────┐
     └────────────────────────────────────│  SQLite   │
                                          │  events   │
   Devin API  ◀───────────────────────────│  tasks    │
   (sessions)                             │  runs     │
                                          │  leases   │
   Slack incoming webhook ◀───────────────│  outbox   │
                                          └───────────┘
                                                ▲
                                                │ claim / lease
                                          ┌───────────┐
                                          │  worker   │
                                          └───────────┘
```

Two processes run under Docker Compose against a shared database volume:

| Process | Responsibility |
| --- | --- |
| `api` | Receives webhooks, validates and persists them, serves the report and health endpoints. Performs no external calls on the request path. |
| `worker` | Claims queued work under a lease, creates and polls Devin sessions, recovers orphaned sessions by tag, drains the notification outbox, and when idle re-reads one active run's issue and PR from the GitHub API (§6). |

GitHub remains the source of truth for issues, pull requests, checks, reviews and
merges. The database holds our own task state and a cache of the last observed
GitHub state; on any disagreement, GitHub wins. The service learns GitHub state
from webhooks first, and with `GITHUB_MODE=live` also from read-only API
re-reads that repair missed deliveries (see *Reconciliation* in §6, D-041). The
REST arrow in the diagram is `GET` only: the service never merges, pushes,
approves or comments.

### Webhook directions

There are exactly two webhook connections, and they are unrelated to each other:

- **Inbound**: GitHub → `POST /webhooks/github`. Signed, verified, deduplicated.
- **Outbound**: notification worker → a configured Slack incoming-webhook URL.

GitHub never calls Slack directly. The application decides destination, content
and timing, because only it holds task context (which issue, which run, which
approver). No Slack Events API subscription is required for outbound-only
notifications; that is only needed later, for interactive actions.

## 2. Domain model

Three nouns, deliberately distinct:

- **Task** — the durable record of one GitHub issue we are tracking. Created at
  intake. Long-lived. One per `(repo, issue_number)`.
- **Run** — one authorized attempt to do the work. Created at approval. A task
  may have several runs over its life (a rerun after a failure), but at most one
  active run at a time.
- **Session** — the Devin session belonging to a run. One per run.

Separating task from run is what makes rerun safe: a rerun is a new row with a
new ID, never a mutation of the previous attempt, so the history of what was
attempted and what it cost stays intact.

## 3. Task state machine

```
                        issues.opened
                              │
                              ▼
                     ┌─────────────────┐
                     │ awaiting_approval│◀──────── label removed
                     └─────────────────┘            (pre-execution)
                              │
              authorized `devin-ready` label
                              │
                              ▼
                        ┌──────────┐
                        │  queued  │──── issue closed / label removed ──▶ cancelled
                        └──────────┘
                              │ worker claims lease
                              ▼
                        ┌──────────┐
                        │ starting │──── Devin session create fails ──▶ failed
                        └──────────┘
                              │ session_id returned
                              ▼
                        ┌──────────┐
             ┌──────────│ running  │◀───── approval revoked
             │          └──────────┘       (flag only, run continues)
    session blocked           │
             │                │
             ▼                │
      ┌───────────────┐       │
      │session_blocked│       │
      └───────────────┘       │
             │                │
             │ resolved       │
             └────────────────┤
                              │
       ┌──────────────────────┼──────────────────────┐
       │                      │                      │
  PR correlated       session finished,        session expired
       │              no PR after grace              │
       ▼                      ▼                      ▼
  ┌──────────┐          ┌──────────┐           ┌──────────┐
  │ pr_open  │          │ no_output│           │ expired  │
  └──────────┘          └──────────┘           └──────────┘
       │
       │  pull_request.closed
       ├───────────── merged=true ──────────▶ merged  (terminal)
       └───────────── merged=false ─────────▶ closed_unmerged (terminal)
```

Terminal states: `merged`, `closed_unmerged`, `cancelled`, `failed`, `no_output`,
`expired`. Everything else is live, and the worker keeps polling its Devin
session.

### State table

| State | Meaning | Exits via |
| --- | --- | --- |
| `awaiting_approval` | Issue tracked, no authorized label yet | authorized label |
| `queued` | Approved, waiting for a worker | worker lease |
| `starting` | Creating the Devin session | session created / create error |
| `running` | Session working | PR seen, session terminal, revocation |
| `session_blocked` | Devin reports `blocked`, needs a human | human resolves, or timeout |
| `pr_open` | PR correlated to the run, humans own it now | PR closed |
| `no_output` | Session finished with no PR | rerun |
| `expired` | Session expired | rerun |
| `failed` | Our own error creating or tracking the run | rerun |
| `awaiting_review` | Verified on the latest head, waiting on a human | PR closed |
| `merged` | PR merged, merge SHA recorded | — |
| `closed_unmerged` | PR closed without merge | — |
| `cancelled` | Approval withdrawn or issue closed **before execution** | rerun |

`awaiting_review` exists in the schema but is **unreachable in v1**: it is
entered only by check evaluation, which is gated off (D-011). With the gate off,
a task stays in `pr_open` from the PR opening until it closes. The state is
defined now so that enabling verification later is a flag and a transition, not
a migration. The dashboard nevertheless *reports* a `pr_open` run whose
current head is *verified* — checks passed and, when `REVIEW_GATE_MODE` is
`required`, Devin Review clear (§8b) — under "awaiting review": a report-time
derivation, not a state change (D-030, D-033).

Revocation after execution has started is a flag on the run, not a state: see
§5 Revocation and `decisions.md` D-006.

Three states exist specifically because the unhappy paths are the common ones:
`session_blocked`, `no_output` and `expired`. Without them a task that produces
no PR sits in `running` forever and nobody is told.

### Transition rules

1. **Do not rely on delivery order.** Every transition is guarded by the current
   state, not by the assumption that the previous event arrived. A
   `pull_request.opened` for a task still in `starting` is legal and promotes it
   straight to `pr_open`.
2. **Transitions are idempotent.** Re-applying the same event to a task already
   in the destination state is a no-op that returns success, so webhook
   redelivery is harmless.
3. **Backwards transitions are rejected**, except the explicit rerun action.
   A late-arriving `issues.labeled` cannot move a `merged` task back to `queued`.
4. **Terminal means terminal.** New work needs a new run ID.

## 4. End-to-end stages

| Stage | Trigger | Action | Evidence |
| --- | --- | --- | --- |
| Intake | `issues.opened` | Store issue metadata and labels | Task in `awaiting_approval` |
| Approval | Authorized `devin-ready` | Verify actor and eligibility, create run | Run ID, approver recorded |
| Execution | Worker claims lease | Create Devin session, poll | Session ID and URL |
| Implementation | Devin works | Reproduce, test, fix, open PR | Branch, PR, test evidence |
| PR notification | `pull_request.opened` | Correlate, update task, enqueue Slack | "PR opened" message |
| Checks | `check_suite` for the current head | Record per-head check state | `checks_state` on the exact head |
| Review gate | Worker, idle tick, PR head known | Ask Devin Review once per head, read its progress; bot verdict arrives from GitHub | `pr_reviews` row for the exact head (§8b) |
| Review | Reviewer acts in GitHub | Record decision | Review state |
| Merge | `pull_request.closed` + `merged=true` | Record merge SHA | "PR merged" message |
| Closure | `pull_request.closed` + `merged=false` | Record outcome | Accurate status |

An issue opened with `devin-ready` already present goes through the identical
authorization policy as a label event — the label's presence is never sufficient
on its own, the actor who put it there is what is checked.

## 5. Intake and authorization

### Credential

A **GitHub App**, installed on the allowlisted repositories, not a repository
webhook plus a personal token: per-installation tokens, a clean permission
boundary, and no dependency on one person's account for a service that
authorizes spend.

Permissions are least-privilege and deliberately exclude merge: Issues
(read/write), Pull requests (read), Contents (read), Checks and Commit statuses
(read, unused until check evaluation lands), Metadata (read). The App cannot
push code and cannot merge — D-005 says the pipeline must not merge, and this
makes it unable to. Devin pushes its own branch under its own GitHub
authentication.

Events subscribed: `issues`, `pull_request`, `pull_request_review`,
`pull_request_review_comment` (the Devin bot's inline findings, counted per
kind) and `check_suite`. Check suites are recorded per head SHA and folded into a
per-run `checks_state`; they never drive a transition (D-011), only the
dashboard's *verified* count (D-030).

### Request path

On every inbound request, in order:

1. Verify `X-Hub-Signature-256` as HMAC-SHA256 over the **exact raw body**, using
   a constant-time comparison. Read the raw bytes before any JSON parsing; a
   re-serialized body will not match.
2. Require an allowlisted repository and a supported event/action pair. Anything
   else is acknowledged with `204` and dropped, so GitHub does not retry.
3. Deduplicate on `X-GitHub-Delivery`. A repeat delivery is recorded and ignored.
4. Persist the event and any resulting task/run/outbox changes in one
   transaction.
5. Return promptly. No Devin call and no Slack call happens on this path.

### Authorization for `devin-ready`

The label is a spend authorization on a public repository, so it is checked
against the actor, not the label:

- The `sender` of the label event must be in an explicit maintainer allowlist.
  Repository-permission checking (`write` / `maintain` / `admin`) is written
  behind the same interface but disabled in v1, so the switch is configuration.
- Only three events are read as that actor's decision: `labeled` with
  `devin-ready`, `opened` with the label already present, and `reopened` by an
  allowlisted maintainer (D-038). `edited` and unrelated label events on an
  issue that already carries the label never re-run the gate, so a label
  applied by someone without authority is not adopted by a later maintainer
  action.
- Eligibility beyond that is purely mechanical: repository allowlisted, issue
  open, label present. There is no content check on the issue body in v1 —
  applying the label *is* the maintainer asserting the issue is specified
  enough (D-016).
- Deliveries are applied in the order GitHub's snapshots say they happened,
  not the order they arrived. Every `issues` payload carries the issue as it
  was when the event fired; a delivery whose `issue.updated_at` is older than
  the newest one already applied to the task is recorded as stale and changes
  nothing (D-040). Without this, a `closed` overtaken by the `reopened` that
  followed it would cancel the run the reopen had just approved.
- The issue is *not* re-fetched from the API immediately before starting. The
  ordered webhook snapshot is what the worker starts from; an issue closed or
  unlabelled after that snapshot is caught if its delivery arrives, or the
  reconciler's next read lands (§6, D-041), before the session is created.
  The window is bounded by `RECONCILE_INTERVAL_SECONDS`, not closed.
- The approving actor and the time of approval are stored on the run.
- An issue comment never authorizes work, from anyone.

If authorization fails, the event is recorded with the reason and the task stays
in `awaiting_approval`. It is not an error and is not retried.

### Revocation

Removing the label or closing the issue **before** execution moves the task to
`cancelled`, and nothing is spent.

After execution has started, the run is **allowed to finish**. Revocation is
recorded on the run as `approval_revoked_by` / `approval_revoked_at`, and a
`needs_human` notification reports that approval was withdrawn mid-run and the
session is continuing. If the session produces a PR it is correlated and
announced as normal, with the revocation noted so the reviewer sees it before
merging.

The reasoning is that a session is usually near-complete by the time anyone
reacts, and the spend is sunk either way, so stopping discards the work without
recovering the cost.

Two consequences worth stating plainly:

- The label is a spend authorization only at the moment the run starts. Once
  started, the only ceiling on a run is `max_acu_limit` (§8, §13), which makes
  that limit the real control rather than a secondary one — particularly since
  there is no documented endpoint that stops a running session at all.
- Stopping our polling would stop our observation, not the work and not the
  spend, so it is never used as a substitute for stopping a session.

## 6. Concurrency and durability

- **One active run per issue**, enforced in the database by a partial unique
  index over the active states, so historical runs never collide with it.
- **Leases, not locks.** A worker claims a run by writing `lease_owner` and
  `lease_expires_at`; a crashed worker's lease simply expires and the run is
  reclaimed. Every lease-holding operation re-checks it still owns the lease
  before writing.
- **External calls stay outside transactions.** Read state, commit, call out,
  commit the result. A transaction is never held open across a network call.
- **Outbox for notifications.** Slack messages are rows committed in the same
  transaction as the state change that produced them, then delivered by the
  worker with retries. This is what makes "state changed but Slack never heard"
  impossible.
- **Reconciliation.** Every live run is compared against current Devin and
  GitHub state on an interval, as the safety net for missed, dropped or
  out-of-order deliveries and for recovery after downtime. The Devin half is
  the session poll and tag-based orphan recovery (§8). The GitHub half
  (`app/reconcile.py`, D-041) runs on worker ticks that found no queued work,
  plus once at startup for every active run: it fetches the run's issue
  (state, labels, events for the actor), discovers the PR by the recorded
  branch, then reads the PR (state, merged, draft, head), the check suites for
  the current head, the reviews and the base branch's required checks. Each
  difference from what the run recorded is turned into the webhook payload
  GitHub would have sent and replayed through `Intake.handle` under a
  deterministic `reconcile:` delivery id, so dedupe, D-040 ordering, terminal
  protection, revocation and notifications all happen in the one place they
  already live. A snapshot never authorizes spend: a `devin-ready` label seen
  on re-read is replayed only with the actor from the issue's event log, for
  the allowlist to judge (§5); an event log longer than the client reads
  (1,000 events, oldest first) names nobody. Terminal runs are never re-read,
  so a PR found already closed is replayed as GitHub lived it — open,
  head, suites, reviews, then closed — before the run turns terminal. One run
  per tick (busy or idle), oldest read first, spaced by
  `RECONCILE_INTERVAL_SECONDS`;
  `runs.reconciled_at` is the bookkeeping and leaves `updated_at` alone. A
  read failure is logged, written to the `reconciler` heartbeat and skipped
  until the next interval; it never blocks run advancement, the review gate
  or the outbox. `GITHUB_MODE=off` (default) is webhook-only; sim and tests
  use `SimulatedGitHubClient`. Out-of-order `issues` deliveries are ordered
  by the payload's own `issue.updated_at` (§5, D-040); out-of-order
  `pull_request` events by the terminal-state rule (§11).
- SQLite in WAL mode with a busy timeout, on a persistent volume. Move to
  PostgreSQL before running multiple hosts.

## 7. PR correlation

Three signals, converging on one link, first writer wins.

**Identity is established at session creation, before any PR exists**, so that
correlation is a lookup rather than a guess:

- Branch convention `devin/issue-<number>-<run-id>`, instructed in the prompt.
- PR body marker `<!-- automation-run: <run-id> -->`, instructed in the prompt.
- Session tag `run:<run-id>` and `repo:<owner>/<name>`, set via `tags` on the
  create call. This one is ours, not Devin's, so it cannot be lost or reworded.

**Signals**:

| Source | Carries | Trust |
| --- | --- | --- |
| `pull_request.opened` webhook | repo, head branch, body, issue refs | authoritative for PR existence |
| Session poll `pull_requests[]` | `pr_url`, `pr_state` | authoritative for "this session opened it" |
| Session tag lookup | session ↔ run | recovery only |

A PR is attached to a run only when **the repository matches an allowlisted
repository for that run AND at least one of (head branch equals the run's branch,
body contains the run marker) AND the run is in a state that can accept a PR**.
A title match alone, or a marker alone on an unexpected repository, is never
sufficient — the marker is public text on a public repository and anyone can
copy it into their own PR body.

The worker records the run's branch when it starts the session, before Devin
can push, so a webhook that arrives before the session poll still matches. A
`pull_request` event that matches no run is recorded in `deliveries`
(`accepted = 0`, with its reason) and is not re-evaluated as a delivery. If
the PR is on a run's recorded branch, the reconciler finds it on its next read
by that branch and replays it as `opened` (§6, D-041); a PR on any other branch
stays unmatched, and its body stays in `deliveries` for a human to inspect
until `DELIVERY_RETENTION_DAYS` clears it (Q-012).

The two sources take different routes (D-007). Only the webhook, through
`Intake._correlate`, writes `pr_number` and `head_sha` and queues "PR opened".
The poll moves a running run to `pr_open` from the session's own
`pull_requests[]` and records `pr_url`. After that, GitHub events own the run.
The "PR opened" fingerprint is keyed on the head SHA, so a webhook redelivery or
a race with the poll still produces one message.

**`pull_requests` is an array.** A session can open more than one PR. The first
correlated PR is the run's primary PR and drives the state machine; any
subsequent one is recorded against the run and reported, but does not create a
second lifecycle. This is a scope signal worth seeing, not an error: each extra
PR, whether the session poll or a same-repository `pull_request` webhook sees it
first, sends one `needs_human` with reason `scope` into the run's PR thread.

**A PR after `no_output`.** A same-repository PR on the recorded branch of a
run already closed as `no_output` is not attached: terminal means terminal.
Instead its first `opened`, `reopened` or `ready_for_review` event sends one
`needs_human` with reason `late_pr`, top-level because the run has no PR
thread. The PR is then outside the pipeline, and a human reviews it or closes
it. PRs naming a `failed`, `expired` or `cancelled` run are only recorded. The
branch is required: the marker is public text, so a copied marker on another
branch alerts no one.

## 8. Devin execution contract

All provider specifics live in `devin_client.py` behind an interface with two
implementations, live and simulated. The rest of the application never sees a
Devin field.

### Endpoints (verified against current docs)

| Operation | Endpoint |
| --- | --- |
| Create session | `POST /v3/organizations/{org_id}/sessions` |
| Get session | `GET /v3/organizations/{org_id}/sessions/{devin_id}` |
| List sessions | `GET /v3/organizations/{org_id}/sessions` |
| Send message | `POST /v3/organizations/{org_id}/sessions/{devin_id}/messages` |
| Append tags | `POST /v3/organizations/{org_id}/sessions/{devin_id}/tags` |
| Archive | `POST /v3/organizations/{org_id}/sessions/{devin_id}/archive` |
| Delete | `DELETE /v3/organizations/{org_id}/sessions/{devin_id}` |
| Org daily consumption | `GET /v3/organizations/{org_id}/consumption/daily` |
| Session daily consumption | `GET /v3/organizations/{org_id}/consumption/daily/sessions/{session_id}` |
| Session insights | `GET /v3/organizations/{org_id}/sessions/{devin_id}/insights` |
| Request insights | `POST /v3/organizations/{org_id}/sessions/{devin_id}/insights/generate` |
| Org session counts | `GET /v3/organizations/{org_id}/metrics/sessions?service_user_ids=…` |
| Org PR counts | `GET /v3/organizations/{org_id}/metrics/prs?service_user_ids=…` |

Authentication is a **dedicated service user**, not an individual's personal
token — the pipeline outlives any one person's account, and a credential that
expires with someone's access fails as sessions that silently stop being
created. It holds `UseDevinSessions` (`org.devins.use`) for the session
endpoints and `ViewOrgConsumption` for the consumption ones.

The two consumption endpoints are billing-aligned and bucketed by day at
midnight PST, so they are the source for a pipeline-wide spend view.
`acus_consumed` from the session poll is the per-run figure used in the report
and for tuning the ceiling (§13); summing it across runs will not agree with
the invoice, and the endpoint is the one that does.

### Analytics: read beside the run, never applied to it

The last four endpoints are the *analytics* read. The worker, after advancing
work and draining the outbox, reads at most one session's insights and daily
consumption per tick into `session_insights`, and on a schedule reads the
organization's session and PR counts — scoped to the service-user IDs the
insights reported, so a human's sessions in the same organization are never
counted — into `provider_metrics`. Both tables are keyed by `env`.

Nothing read here drives a transition or a notification. It is displayed as
Devin's account of the run: what it cost, how large the provider judged the
session, what the provider's analysis recommends (`action_items` typed
`machine_setup | repo_config | knowledge | prompt_improvement`, `skill_usage`)
and, in Integration health, whether the provider's counts agree with what
GitHub told this service. A disagreement is shown as drift, not reconciled
(D-032). A failed read stores `last_error` beside the previous good figures and
is surfaced on the page; it never touches the run.

Three ACU figures exist for one session and are never mixed silently: the
poll's `acus_consumed` (a running snapshot), the insights total (the
provider's post-hoc figure) and daily consumption (billing-grade, published at
Pacific midnight, often hours late). The dashboard prefers billing, then
insights, then poll, and names which one it is showing. A session with no
figure is *pending*, not zero.

### There is no documented stop endpoint

Archive and delete are record operations, not "stop the work and stop the
spend". Nothing in the documented surface cancels a running session. The
available controls are therefore:

1. `max_acu_limit` on the create call — a provider-enforced ceiling, set before
   the work starts. This is the real spend control.
2. A message to the session asking it to stop — cooperative, not guaranteed.
3. Our own application timeout — stops *our* tracking only.

These three are different things and the report must not conflate them. This is
also the practical reason the pipeline lets a revoked run finish (D-006): there
is no clean cancel to invoke.

### Session creation

The create call sets, at minimum:

| Field | Value | Why |
| --- | --- | --- |
| `prompt` | rendered task contract, below | the work |
| `tags` | `run:<run-id>`, `repo:<owner>/<name>`, `issue:<n>`, `env:<live\|sim>` | correlation and orphan recovery |
| `max_acu_limit` | per-run ceiling from config | the only hard spend limit |
| `title` | `#<issue> <issue title>` | legible session list |
| `secret_ids` | explicit minimal list | see below |
| `playbook_id` | the bug-remediation playbook | see below |
| `structured_output_schema` | schema below | machine-readable result |

`secret_ids` defaults to *all* organization secrets when omitted. Pass an
explicit list — empty if the repository needs none. Issue text is task data, not
authorization, and the narrowest credible way to enforce that is to not hand the
session credentials it has no use for.

The standing requirements (read the contributing guide, reproduce, add a failing
regression test, focused change on a task branch, run the checks, open a PR with
cause and evidence, leave merge to a maintainer) belong in a **playbook**, not
in every prompt. The prompt then carries only what varies per run: repository,
issue snapshot, baseline revision, run ID, branch name, PR marker, acceptance
criteria and allowed scope. Keeping the invariant part in a playbook means it is
versioned in one place and the prompt diff between two runs is the task.

Repository-specific knowledge — how to stand up the target application, which
hosts it fetches from, which fixtures a reproduction needs — lives in **skills
committed to the target repository** under `.agents/skills/`, not in this
service. A session picks them up from the checkout it is working in, so the
pipeline stays repository-agnostic and each repository's runtime knowledge is
versioned and reviewed where it applies (D-031).

### Structured output

v3 supports `structured_output_schema` (JSON Schema draft 7) and
`structured_output_required`, and returns a validated `structured_output` on the
get endpoint. Use it rather than parsing prose:

```json
{
  "run_id":            "string",
  "reproduced":        "boolean",
  "reproduction_note": "string",
  "branch":            "string",
  "pr_url":            "string | null",
  "regression_test":   "string | null",
  "tests_run":         "string",
  "outcome":           "fixed | blocked | not_reproducible"
}
```

Two cautions:

- This is the *session's own account of itself*. It is useful for the report and
  for the "reproduction blocked" path, which has no GitHub-observable evidence
  at all. It is not verification.
- Correlation still runs off the branch, marker and repository checks above.
  Do not attach a PR to a run because `structured_output.pr_url` said so.

(Note: v1's `structured_output` is typed `null`; only v3 returns it populated.)

### Status mapping

v3 reports status in two dimensions, and the useful information is mostly in the
second:

- `status`: `new`, `claimed`, `running`, `exit`, `error`, `suspended`, `resuming`
- `status_detail`: when running — `working`, `waiting_for_user`,
  `waiting_for_approval`, `finished`; when suspended — a reason such as
  `inactivity`, `user_request`, `usage_limit_exceeded`, `out_of_credits`,
  `out_of_quota`, `org_usage_limit_exceeded`, `contract_expired`, `error`

Note that `finished` is a *detail under `running`*, not a terminal status. Any
mapping that only reads `status` will miss task completion entirely.

| `status` / `status_detail` | Our state |
| --- | --- |
| `new`, `claimed` | `starting` |
| `running` / `working` | `running` |
| `running` / `waiting_for_user` | `session_blocked` — notify, session URL is the action |
| `running` / `waiting_for_approval` | `session_blocked`, reason `approval` |
| `running` / `finished`, PR correlated | `pr_open` |
| `running` / `finished`, no PR after grace | `no_output` — notify |
| `exit` | terminal; `pr_open` if correlated, else `no_output` |
| `error` | `failed` — notify |
| ACU ceiling reached | `failed`, reason `acu_limit` — never auto-retried (§13) |
| `suspended` / `inactivity`, `user_request` | `session_blocked`, resumable by message |
| `suspended` / quota, credit or contract reason | `failed`, reason `capacity` — operator problem, not an issue problem |
| `resuming` | previous state retained |

Both raw fields are persisted verbatim on the run alongside our own state. The
provider's vocabulary and ours are never merged into one column: when they
disagree, we need to be able to see that they disagree.

Quota and credit suspensions are called out separately because they are not
failures of the task. Announcing "the fix failed" when the organization is out
of credits sends a maintainer to read a diff that does not exist.

### Polling and orphan recovery

Polling is on a backoff and refreshes the lease each cycle. `acus_consumed` is
recorded on every poll, which gives the report real cost per issue.

If a create call times out ambiguously, **do not retry blind**. List sessions
filtered by the `run:<run-id>` tag: either the session exists and is adopted, or
it does not and creation is safe. This is why the tag is set at creation rather
than appended afterwards. If the listing itself is unavailable, the run goes to
`failed` with reason `uncertain_create` for a human to inspect — never a second
create.

### A finished session is not a fix

Session completion is evidence that Devin believes it is done. Merge-readiness
is established through GitHub: the PR exists, on the expected repository and
branch, and the checks on its *latest head SHA* are what they are. The pipeline
reports both and conflates neither.

## 8b. Review gate (D-033)

After `pr_open`, every pipeline PR gets an independent reading by Devin Review
before the pipeline will call it verified. The gate has one mode setting and
three separate facts, and the design is mostly about keeping the facts apart.

### Lifecycle

```text
PR head known (pull_request.opened / synchronize)
  └─ worker, idle tick: POST /pr-reviews {pr_url}          once per (pr_url, head)
       └─ GET /pr-reviews?pr_url&commit_sha=<head>          every REVIEW_POLL_SECONDS
            status: pending → running → completed | errored | cancelled | skipped
  └─ GitHub: pull_request_review by devin-ai-integration[bot] on that commit
       body "found N potential issues" | "no issues found"   → findings, review_url
       inline comments <!-- devin-review-comment {kind} -->  → findings_by_kind
  └─ new head (synchronize): old row kept, event `review_gate superseded`,
     new row + new request for the new head
```

The worker asks and reads only on ticks where no run advanced, so a slow
provider never delays intake, session polling or Slack. It stops reading a head
as soon as a GitHub verdict exists for it, when the API reports a terminal
status, or after `REVIEW_MAX_ATTEMPTS` consecutive provider errors, at which
point the head is `unavailable` with the last error stored. Every request,
status change, verdict, error and supersession is a durable `review_gate` event
in the run's timeline.

### Three facts, one row per head

| Fact | Source | Columns | Answers |
| --- | --- | --- | --- |
| Provider progress | `pr-reviews` API | `status`, `status_at`, `attempts`, `last_error` | Has Devin reviewed this commit yet? |
| Verdict | GitHub review by the Devin bot | `findings`, `findings_by_kind`, `review_url`, `verdict_at` | What did it find on this commit? |
| Human review | GitHub review by a person | `runs.review_state`, `runs.reviewer` | Does a maintainer accept the change? |

A `completed` status with no verdict is *awaiting verdict*, never clear. A
verdict without a status is a verdict. A row is never re-polled once it has a
verdict, so a later API answer cannot overwrite what GitHub said. If the API
reports a review of a commit the run does not know, that is recorded as the
run's head being skipped and the pipeline waits for GitHub to report the new
head — the provider's view of a PR is not used to update the run's head.

### Current head only

`Store.review_for_head(run_id, runs.head_sha)` is the only read that feeds
verification. Rows for earlier heads remain for audit and appear in the
timeline as *superseded*; they count toward nothing. This is the same rule the
check suites follow (D-030), applied to a second kind of evidence.

### What "verified" means per mode

| `REVIEW_GATE_MODE` | Review calls | `verified` |
| --- | --- | --- |
| `off` | none | checks passed on the current head |
| `advisory` | ask + read, shown everywhere | checks passed on the current head (review shown, not required) |
| `required` (default) | ask + read | checks passed **and** review clear, both on the current head |

Findings do not fail a run. It stays `pr_open` and unverified, the count and
kinds are on the task row and in the timeline, and the follow-up — fix in the
PR, dismiss with a reason on GitHub, or open a follow-up issue through the
normal gate — is a human decision. Unknown, errored, skipped, unavailable and
not-yet-reviewed are all "not clear". Nothing here changes who merges: the
human review and the merge stay in GitHub and are shown separately (§12).

### Failure isolation

A provider error is stored on that head's row, logged, and counted against
`REVIEW_MAX_ATTEMPTS`; it is never raised into the worker loop and never
touches another run. Permission failures (403) are not retried — as elsewhere
(§11), a 403 is a configuration problem. A run whose head is `unavailable`
appears in *Needs attention* with the reason and the action.

### Repository-wide scans are not this

Code Scans / Security Swarm read a whole repository, run as sessions and can
propose remediation PRs. When used, their findings enter as GitHub issues that
go through the `devin-ready` maintainer gate like any other request; the
pipeline does not call `remediate` and nothing they propose merges on its own.

## 9. Slack notifications

### Transport

Two transports, chosen by `SLACK_TRANSPORT`:

- **`webhook`** — a Slack app with Incoming Webhooks enabled, one secret
  webhook URL per destination (`SLACK_DESTINATIONS`). Top-level messages only:
  a webhook returns no message id, so it can neither thread nor react.
- **`bot`** — the same app installed with a bot token (`SLACK_BOT_TOKEN`,
  scopes `chat:write` and `reactions:write`), one channel id per destination
  (`SLACK_CHANNELS`). `chat.postMessage` returns the message `ts`, which
  enables the threaded lifecycle below (D-036). The bot must be invited to
  each channel. With `channels:history` (and `groups:history` for private
  channels) the bot can also find a *PR opened* post that went out through a
  webhook, so runs that straddle a switch from `webhook` to `bot` still get a
  thread (D-042); without it those runs' follow-ups stay top-level.

In both, each allowlisted repository maps to an approved **destination key**,
and only the key resolves to a URL or channel id. A destination is never
derived from issue or PR content — untrusted text from a public repository
must not be able to influence where a message goes, and cannot name a channel
that is not already in the configuration. With webhooks Slack enforces this
(the URL is bound to its channel); with the bot the configuration map is the
only resolver and there is no code path from payload to channel.

Suggested destination keys: `engineering-updates` and `automation-alerts`. These
are logical routes in configuration, not channels to create or post to without
the workspace owner's authorization.

Default scope: PRs the pipeline owns. All-repository PR notification is a later,
explicit setting.

### Policy

| Event | Message | Delivery rule |
| --- | --- | --- |
| Tracked PR opened | PR opened, draft state, current checks | Immediately, even with checks pending. **Anchor** for everything below |
| Draft → ready | Ready-for-review intent | Must not imply CI passed |
| Checks fail on the current head | Failed, next action | Once per head, on the first `failed` derivation; superseded heads are silent (D-036 amends D-011) |
| Devin Review finds issues | Finding count, next action | Once per head, from the bot's review on GitHub |
| Verification passed | Verified, ready for human review | Checks passed and review gate satisfied on the current head, once per head |
| Human review submitted | Approved / changes requested, by whom | Once per review id; comments alone are not announced |
| Run blocks or fails | Reason and required action | On state transition only, never per poll |
| PR merged | Merged, merge SHA, issue reference | Requires GitHub `merged=true` |
| PR closed unmerged | Closed without merge | Distinct from success |

### Threads and reactions (D-036)

A channel that receives every lifecycle event as a separate line is unread.
With the bot transport the run's *PR opened* message is its **anchor**: the
worker stores the channel and `ts` Slack returned on the outbox row, and every
later message for the same run and destination is posted as a reply in that
thread (`thread_ts`) and adds one reaction to the anchor. The channel reads one
line per PR whose emoji tell its fate; the thread holds the record.

| Follow-up | Reaction on the anchor |
| --- | --- |
| Ready for review | :eyes: |
| Checks failed | :red_circle: |
| Devin Review findings | :mag: |
| Verified | :large_green_circle: |
| Human approved / changes requested | :thumbsup: / :pencil2: |
| Needs human | :warning: |
| Merged | :white_check_mark: |
| Closed unmerged | :no_entry_sign: |

Rules, in order:

1. **Anchor pending → follow-up waits.** If the *PR opened* row is still
   `pending` (retrying), follow-ups for that run are left in the outbox until
   it is sent, so the thread is never started by its second message.
2. **No usable anchor → top-level.** Webhook transport, an anchor that failed
   for good, or rows from before anchors existed: the follow-up is sent as an
   ordinary message rather than lost. Messages before a PR exists (session
   started, needs-human while running) are always top-level.
3. **Reaction is best effort, once.** It runs after the reply is recorded as
   sent; a failure (`missing_scope`, channel access) is stored on the reply's
   row and shown in the timeline, not retried — the reply already carries the
   information. `already_reacted` is success.
4. **Slack holds no state GitHub does not.** The anchor is a place to put
   words. No run transition, verification or dashboard fact reads it, and a
   lost anchor changes nothing but message placement.

Suppression fingerprints (below) are unchanged: threading decides *where* a
message goes, fingerprints decide *whether* it goes at all.

"Ready for review" is a draft-status change and says nothing about quality.
Missing checks are reported as `unknown`, never as passed. These two are the
failure modes that make a notification channel actively harmful, because they
look like verification and are not.

### Suppression

Every notification carries a **fingerprint** over `(task, event type,
destination, relevant revision or state)`. A fingerprint already recorded as
sent is suppressed. The revision component is what keeps a new commit from being
silently swallowed while still collapsing repeated polls of an unchanged state:
the same check result on the same head SHA is one message, the same check result
on a new head SHA is a new one.

There is no per-commit and no per-poll message.

### Message content

Each PR notification carries repository, issue title and number, PR number and
link, run ID, Devin session link, draft/ready state, check status and the next
human action.

```
PR opened: <title>
Repository:   <owner/repository>
Issue:        <issue link>
PR:           <PR link>
State:        <draft|open>  |  Checks: <pending|passed|failed|unknown>
Devin session: <session link>
Run:          <run ID>
Next action:  <wait for checks | review the change>
```

A short plain-text `text` field is always set as the notification fallback;
Block Kit formatting is optional on top of it.

All user-controlled text — issue titles, PR titles, branch names — is escaped,
and `@channel`, `@here` and `<!everyone>` style broadcasts are neutralized
before rendering. On a public repository an issue title is attacker-controlled
input, and an unescaped one is a channel-wide ping from a stranger. Never
include credentials, webhook URLs or raw logs.

### Reliable delivery

The outbox row is written in the same transaction as the triggering transition;
a worker sends it after commit and records attempt count, response, timestamp
and final state. Transient failures and rate limits back off with bounds,
honouring `Retry-After` when Slack returns it. Permanent failures and exhausted
retries surface in the task report rather than disappearing into logs.

Two independence properties:

- **Slack failure never touches the run.** It does not restart a session and
  does not mark a fix failed. Notification status is tracked separately, and a
  merged PR with an undelivered message is still a merged PR.
- **Delivery is at-least-once, not exactly-once.** A timeout after Slack has
  accepted a message is indistinguishable from a timeout before, so a retry can
  duplicate. Retries are bounded, every message carries its run ID so a
  duplicate is recognizable as one, and the limitation is documented rather than
  pretended away.

### Connection checklist

1. Agree workspace and exact channels with the owner.
2. Create or reuse an approved Slack app. Webhook transport: enable Incoming
   Webhooks. Bot transport: add bot scopes `chat:write` and `reactions:write`
   (plus `channels:history` / `groups:history` to adopt webhook-era anchors,
   D-042), install to the workspace, invite the bot to each channel.
3. Authorize a webhook per destination, or note each channel's id (`C…`).
4. Store the webhook URLs or the bot token as secrets; configure
   repository → destination routing and destination → URL/channel.
5. Verify formatting against a local fake endpoint first.
6. Send a live test only after the owner authorizes that specific destination.
7. Confirm delivery and error reporting without exposing the URLs.

Steps 5 and 6 are ordered deliberately: formatting bugs are found against the
fake endpoint, not in somebody's channel.

## 10. Schema

`SCHEMA` in `app/store.py` is the source of truth; this is a copy of it, with
the `{active}` placeholder filled in from `app.states.ACTIVE`. Columns added
after a database was first created are backfilled by `Store._migrate`.

```sql
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,          -- X-GitHub-Delivery
    event       TEXT NOT NULL,
    action      TEXT,
    repo        TEXT,
    received_at TEXT NOT NULL,
    accepted    INTEGER NOT NULL,          -- 0 recorded but not acted on
    reason      TEXT,                      -- why ignored or rejected
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY,
    repo         TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_title  TEXT NOT NULL DEFAULT '',
    issue_state  TEXT NOT NULL DEFAULT 'open',
    labels       TEXT NOT NULL DEFAULT '[]',
    issue_updated_at TEXT,   -- issue.updated_at of the newest applied event
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (repo, issue_number)
);

CREATE TABLE IF NOT EXISTS runs (
    id                    TEXT PRIMARY KEY,   -- run ID, appears in branch and PR marker
    task_id               INTEGER NOT NULL REFERENCES tasks(id),
    state                 TEXT NOT NULL,
    env                   TEXT NOT NULL,      -- live | sim, never mixed in a report

    approved_by           TEXT,
    approved_at           TEXT,
    approval_revoked_by   TEXT,
    approval_revoked_at   TEXT,
    input_snapshot        TEXT,               -- issue as it was at approval

    lease_owner           TEXT,
    lease_expires_at      TEXT,

    session_id            TEXT,
    session_url           TEXT,
    session_status        TEXT,               -- provider status, verbatim
    session_status_detail TEXT,               -- provider detail, verbatim
    session_polled_at     TEXT,
    session_finished_at   TEXT,
    acus_consumed         REAL,
    max_acu_limit         INTEGER,
    structured_output     TEXT,

    branch                TEXT,
    base_sha              TEXT,
    pr_number             INTEGER,
    pr_url                TEXT,
    pr_state              TEXT,
    pr_draft              INTEGER,
    head_sha              TEXT,
    checks_state          TEXT,
    checks_head_sha       TEXT,
    review_state          TEXT,
    merged_sha            TEXT,
    extra_pr_urls         TEXT NOT NULL DEFAULT '[]',

    failure_reason        TEXT,
    reconciled_at         TEXT,               -- last GitHub re-read (D-041)
    required_checks       TEXT,               -- base protection, JSON list (observed)
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

-- At most one active run per issue. Partial, so historical attempts do not
-- collide with a new one.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active
    ON runs (task_id) WHERE state IN ('awaiting_approval', 'awaiting_review', 'pr_open', 'queued', 'running', 'session_blocked', 'starting');

CREATE UNIQUE INDEX IF NOT EXISTS runs_one_pr
    ON runs (task_id, pr_number) WHERE pr_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL REFERENCES tasks(id),
    run_id          TEXT REFERENCES runs(id),
    kind            TEXT NOT NULL,
    reason          TEXT,
    -- Over (task, kind, destination, relevant revision/state): a new head SHA
    -- is a new message, an unchanged poll is not.
    fingerprint     TEXT NOT NULL UNIQUE,
    destination     TEXT NOT NULL,
    payload         TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_response   TEXT,
    next_attempt_at TEXT,
    created_at      TEXT NOT NULL,
    sent_at         TEXT,
    -- Identity Slack gave the message, when the transport reports one. The
    -- run's sent `pr_opened` row is the anchor later PR-lifecycle messages
    -- thread under (thread_ts) and react to (reaction).
    slack_channel   TEXT,
    slack_ts        TEXT,
    thread_ts       TEXT,
    reaction        TEXT,
    reaction_error  TEXT
);

CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (state, next_attempt_at);

-- Append-only history of what happened to a run: every state change, plus
-- the observations (session, PR, checks, review) that a timeline needs.
CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- state|session|pr|checks|verified|
                                           -- review|review_gate|insights|
                                           -- reconcile|protection
    from_state  TEXT,
    to_state    TEXT,
    reason      TEXT,
    detail      TEXT                       -- JSON, kind-specific
);

CREATE INDEX IF NOT EXISTS run_events_run ON run_events (run_id, id);

-- One row per check suite GitHub reported for a head SHA on a tracked run.
-- Verification is derived from all suites on the *current* head, never
-- from a single one.
CREATE TABLE IF NOT EXISTS checks (
    run_id      TEXT NOT NULL REFERENCES runs(id),
    head_sha    TEXT NOT NULL,
    suite_id    TEXT NOT NULL,
    app         TEXT,
    status      TEXT NOT NULL,             -- queued | in_progress | completed
    conclusion  TEXT,                      -- success | failure | ... | NULL
    url         TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, head_sha, suite_id)
);

-- Check suites keyed by repository head, whether or not a run tracks that
-- head yet. GitHub reports suites for a pushed commit before the pull request
-- that carries it is opened; a run that only learns about suites after its PR
-- exists would see the first completed one arrive alone and call it a pass.
CREATE TABLE IF NOT EXISTS head_checks (
    repo        TEXT NOT NULL,
    head_sha    TEXT NOT NULL,
    suite_id    TEXT NOT NULL,
    app         TEXT,
    status      TEXT NOT NULL,
    conclusion  TEXT,
    url         TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (repo, head_sha, suite_id)
);

-- Liveness of the processes that are supposed to be running. A dashboard
-- that cannot tell "nothing happened" from "nobody is looking" is useless.
CREATE TABLE IF NOT EXISTS heartbeats (
    component   TEXT PRIMARY KEY,          -- worker | api | reconciler
    owner       TEXT,
    at          TEXT NOT NULL,
    detail      TEXT
);

-- Devin's account of one run's session: cost, size, and its own analysis of
-- what went well or badly. Kept in its own table because it is provider
-- testimony, refreshed on a schedule, and none of it changes a run's state.
CREATE TABLE IF NOT EXISTS session_insights (
    run_id              TEXT PRIMARY KEY REFERENCES runs(id),
    session_id          TEXT NOT NULL,
    env                 TEXT NOT NULL,
    fetched_at          TEXT NOT NULL,
    fetch_count         INTEGER NOT NULL DEFAULT 1,
    run_state           TEXT,                -- run state at fetch, drives refresh
    session_status      TEXT,
    session_status_detail TEXT,
    acus_consumed       REAL,                -- session's running total
    billed_acus         REAL,                -- daily consumption, billing-grade
    consumption         TEXT NOT NULL DEFAULT '[]',   -- JSON [[day, acus], ...]
    session_size        TEXT,                -- xs | s | m | l | xl
    category            TEXT,
    subcategory         TEXT,
    origin              TEXT,
    service_user_id     TEXT,
    num_user_messages   INTEGER,
    num_devin_messages  INTEGER,
    analysis_status     TEXT,                -- started | completed | failed
    analysis            TEXT,                -- JSON, verbatim
    generate_requested_at TEXT,
    settled             INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT
);

-- Devin Review of one PR head (D-033). One row per (run, commit): a new head
-- gets a new row and the old one stays as history, so a verdict can never be
-- read against a commit it was not given for. `status` is what the pr-reviews
-- API says about progress; `findings` is what GitHub carries as the bot's
-- review of that commit. Only the latter can clear a head.
CREATE TABLE IF NOT EXISTS pr_reviews (
    run_id          TEXT NOT NULL REFERENCES runs(id),
    head_sha        TEXT NOT NULL,
    env             TEXT NOT NULL,
    pr_url          TEXT NOT NULL,
    status          TEXT,                    -- pending|running|completed|errored|
                                             -- cancelled|skipped|unavailable
    requested_at    TEXT,                    -- when this worker asked for it
    status_at       TEXT,                    -- last API read
    attempts        INTEGER NOT NULL DEFAULT 0,  -- failed API calls in a row
    retryable       INTEGER NOT NULL DEFAULT 1,  -- 0: last error was permanent
    last_error      TEXT,
    findings        INTEGER,                 -- from the bot's GitHub review summary
    findings_by_kind TEXT NOT NULL DEFAULT '{}',  -- JSON kind -> count
    review_url      TEXT,                    -- the bot's review on GitHub
    verdict_at      TEXT,
    PRIMARY KEY (run_id, head_sha)
);

-- The latest organization-level counts Devin reports for the pipeline's own
-- service user, kept per environment so the drift check against the store
-- compares like with like.
CREATE TABLE IF NOT EXISTS provider_metrics (
    env                 TEXT PRIMARY KEY,
    attempted_at        TEXT NOT NULL,       -- last read, successful or not
    fetched_at          TEXT,                -- last successful read; window,
    window_after        TEXT,                -- ids and metrics below belong
    window_before       TEXT,                -- to it and move together
    service_user_ids    TEXT,                -- JSON list
    metrics             TEXT,                -- JSON, verbatim
    last_error          TEXT
);
```

Points the DDL alone does not make obvious:

- **Deliveries** record every signed webhook, acted on or not (`accepted`,
  `reason`). The ID is what makes a redelivery a no-op, so pruning (Q-012)
  clears `payload` and keeps the row.
- **One active run per issue.** `runs_one_active` covers every state in
  `ACTIVE`, including `awaiting_approval`. An issue therefore never has an
  approval-pending run beside a queued one.
- **One PR per run.** `runs_one_pr` is unique on `(task_id, pr_number)`, not on
  `pr_number`, because PR numbers are only unique within a repository.
  Correlation (§7) resolves a PR to a single run by its branch or marker.
- **History** is `run_events`: every state change, written in the same
  transaction as the change, plus the session, PR, checks, review and verified
  observations the timeline shows. There is no separate `transitions` table.
- **Checks** are kept twice. `head_checks` holds every suite GitHub reports for
  a commit, whether or not a run tracks it yet; `checks` holds the suites for a
  tracked run's heads. A run adopts suites from `head_checks` when its PR
  appears (D-030).
- **Outbox.** `fingerprint` is the reason a notification cannot be sent twice.
  The insert is part of the same transaction as the change that caused it, and
  a repeat is ignored by the unique constraint. `state` is
  `pending | sent | failed`. `needs_human` rows carry a `reason` from the
  vocabulary in `needs_human_text` (`app/notifications.py`).
- **`heartbeats`** records when each process was last alive, so the dashboard
  can tell "nothing happened" from "nobody is looking".

## 11. Recovery rules

- Persist before calling out; reclaim expired leases.
- An ambiguous session-create is reconciled by tag lookup, never retried blind
  (§8). If reconciliation is unavailable, the run goes to `failed` with reason
  `uncertain_create` for inspection.
- Retry transient reads. Do not retry permanent permission failures — a 403 is
  a configuration problem, and retrying it only delays finding that out.
- Cap concurrent runs, repair attempts and run duration.
- Application timeout, provider suspension and actual usage control are three
  different things (§8) and are reported as three different things.
- **A new PR head invalidates prior verification.** On a new commit, clear
  `checks_state` back to pending for the new `head_sha`, keep the old
  `pr_reviews` row, and start a new one for the new head. Verification is
  always a statement about one specific revision, for checks and for review.
- **A review-provider failure is that head's problem.** It is recorded on the
  row, capped by `REVIEW_MAX_ATTEMPTS`, shown as `unavailable`, and never
  stops the worker or affects another run.
- **A delayed delivery never moves state backwards.** A late `pull_request`
  delivery must not move a merged or closed run back to open: once a run is
  terminal, PR-open actions for it are ignored. A late `issues` delivery must
  not replay a state the issue has already left: the payload's
  `issue.updated_at` is compared with the newest one applied to the task and
  an older snapshot is recorded as stale (§5, D-040). Reconciliation (§6)
  replays through these same rules, and reads active runs only, so a re-read
  cannot move a terminal run either.
- Simulated and live data are visibly separated by the `env` column, and the
  report never mixes them in one figure.

Start with one active session and a configurable poll interval. These are
operational defaults, not guarantees about completion time and not spend limits.

## 12. Human review and merge

The PR opening notifies immediately. Review-readiness requires the configured
evidence on the **latest head SHA**; missing checks are `unknown`, not passed.
GitHub's "ready for review" reflects draft status only.

Reviewers validate behaviour, inspect scope and request changes in GitHub.
Branch protection and manual merge stay in force. After a merge the pipeline
records the merge SHA and the issue's actual state.

Devin Review (§8b) is a second reader, not a second approver. Its verdict is
required for *verified* under the default gate mode, shown separately from the
human review state, and satisfies none of the human's obligations: a PR that is
checks-green and review-clear still waits for a maintainer to read it, approve
it and merge it. The same applies to any PR produced by the overnight sweep
(§16) or proposed by a repository scan.

A later extension may relay an *explicitly approved* change request into the
existing session via the messages endpoint. It will not execute review comments
automatically: a review body is discussion, and on a public repository a comment
is not an instruction.

"Merged" is not "deployed". Deployment verification is a later addition.

## 13. Budget controls

A public repository means unbounded inbound volume, and the approval label is the
only thing between an issue and paid work. Three limits, all enforced before the
session is created:

| Limit | Value |
| --- | --- |
| Concurrent active runs per repository | **2** |
| Sessions started per repository per rolling day | 10 (default, unconfirmed) |
| `max_acu_limit` per run | **20 to start**, then set from measurement |

The third is **set on the create call** rather than enforced by polling, and it
is the important one, because it is provider-enforced. Polling
`acus_consumed` and reacting is a weaker design given there is no documented
stop endpoint (§8): by the time a poll notices an overrun there is no reliable
way to act on it. Setting the ceiling before the work starts is the only
mechanism that is guaranteed to hold.

The 20 is a placeholder, not a derived figure: there is no documented mapping
from ACUs to a unit of work, so the number has to be measured. It is set loose
on purpose, because the failure modes are asymmetric — a ceiling that is too
high wastes the difference, while one that is too low severs a legitimate fix
mid-work and still charges the full ceiling for nothing mergeable. After ~20
completed runs it is reset to about 2× the p90 of runs that produced a merged
PR. A run that hits the ceiling becomes `failed` with reason `acu_limit` and is
not auto-retried; the same retry under the same ceiling fails the same way at
the same price.

Exceeding a concurrency or daily limit leaves the run `queued` with a recorded
reason rather than failing it, and a queued run carries over to the next day
rather than being dropped — its authorization is still valid, and a discarded
run is indistinguishable from a bug to the maintainer who approved it.
`acus_consumed` is still polled and reported, for visibility and for tuning the
ceiling — not as an enforcement path.

## 14. Build order

1. Store, state machine and transitions, against fixtures only.
2. Webhook intake with signature verification and dedupe.
3. Simulated Devin adapter; the whole pipeline green in tests, zero ACUs spent.
4. Outbox and notification formatting, against a local fake Slack endpoint.
5. Live Slack transport, to an owner-authorized destination.
6. Live Devin adapter behind the same interface, with `max_acu_limit` set.
7. Reconciliation loop: tag-based orphan recovery (Devin half), then the
   read-only GitHub client and active-run re-read (GitHub half, D-041).
8. Report endpoint.

Steps 1–4 spend nothing, touch nobody's channel and cover the majority of the
logic. That is the point of the simulated adapter being a first-class component
rather than a test helper.

## 15. Deliberately out of scope for v1

- Automatic merge. Never in v1, including for remediation or sweep PRs.
- Acting on review comments automatically — Devin Review findings included;
  they are shown, and a human decides.
- Narrowing "checks passed" and "review clear" to what branch protection
  requires. The required contexts are now read and recorded per run
  (`runs.required_checks`, D-041) but not consumed: verification is still all
  suites GitHub reported plus the Devin Review verdict on the current head
  (D-030, D-033), and changing that is a separate gate decision.
- Slack interactive actions, message updates, Events API subscription.
- Follow-up instructions to an existing session from a reviewer.
- Similar-bug discovery, feature implementation, deployment verification.

Threading was the one later-release item designed for from the start (D-009);
it shipped as the bot transport in D-036.

## 16. Documentation, DeepWiki and the overnight sweep (D-034, D-035)

Five kinds of text describe this system, and they are not interchangeable:

| Text | Where | Says | Changed by |
| --- | --- | --- | --- |
| Operating guide | `README.md` | how to run, configure and read it | reviewed PR |
| Intended design | `docs/architecture.md` | what the system is meant to do and why the boundaries are where they are | reviewed PR |
| Decision record | `docs/decisions.md` | why each choice was made, what it is not, when to revisit | reviewed PR |
| Runtime instructions for sessions | `.agents/skills/` in the **target** repository | how to stand up and test that repository (D-031) | reviewed PR in that repository |
| Generated description | DeepWiki, steered by `.devin/wiki.json` | what the code does today, with diagrams and source links | regenerated from the code; page tree by reviewed PR |

The README and `docs/` are canonical. DeepWiki is navigation and context: the
place a new engineer starts reading, the corpus Ask Devin and Devin sessions
consult, and never the record of intent. `.devin/wiki.json` names the pages the
wiki should have (overview, lifecycle, authority boundaries, review gate,
security, operations, onboarding) so the generated structure follows the
architecture rather than the directory tree. Reading the wiki creates nothing —
no issue, PR or session. When the wiki and the docs disagree, one of them has a
bug, and it is fixed by a PR.

### Overnight sweep

One scheduled Devin Automation runs nightly against this repository with a
fixed prompt: compare the code with `docs/architecture.md`, `docs/decisions.md`
and the README, look for demonstrable defects, and for each actionable finding
open an issue (needs a decision, or not small) or a PR with a test (small and
clearly right); then post one Slack summary. Bounds: repository scope named in
the prompt, one run per night, no overlap with a still-running previous run,
an ACU cap, no reopening of a finding already raised, and "nothing actionable"
as an expected result.

The sweep's output is a proposal. Its PRs go through CI, the review gate (§8b)
and human review like every other PR; its issues go through the `devin-ready`
maintainer gate before any further spend. It adds no trust path and cannot
change `main` on its own. It is not the per-PR gate, not a security scan (Code
Scans are authorised separately) and not self-healing.
