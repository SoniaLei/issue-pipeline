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
| `worker` | Claims queued work under a lease, creates and polls Devin sessions, reconciles GitHub state, drains the notification outbox. |

GitHub remains the source of truth for issues, pull requests, checks, reviews and
merges. The database holds our own task state and a cache of the last observed
GitHub state; on any disagreement, GitHub wins and reconciliation corrects us.

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
`expired`. Everything else is live and is visited by reconciliation.

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
a migration.

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

Events subscribed: `issues`, `pull_request`, `pull_request_review`, and
`check_suite` / `status` once verification lands.

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
- Eligibility beyond that is purely mechanical: repository allowlisted, issue
  open, label present. There is no content check on the issue body in v1 —
  applying the label *is* the maintainer asserting the issue is specified
  enough (D-016).
- The issue is re-fetched from the API before starting and must still be open and
  still carry the label. A webhook is a hint; the API is the truth.
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
- **Reconciliation loop.** On an interval, every live task is compared against
  current GitHub and Devin state and corrected. This is the safety net for
  missed, dropped or out-of-order deliveries, and it is how the system recovers
  after being down.
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

The webhook usually arrives before the session poll returns a URL, so an
unmatched `pull_request.opened` is not discarded: it is parked in `deliveries`
and re-evaluated by reconciliation once the run has a branch recorded.

Correlation writes `(run_id, pr_number)` under a unique constraint, so a race
between webhook and poll produces one row and one notification.

**`pull_requests` is an array.** A session can open more than one PR. The first
correlated PR is the run's primary PR and drives the state machine; any
subsequent one is recorded against the run and reported, but does not create a
second lifecycle. This is a scope signal worth seeing, not an error.

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

## 9. Slack notifications

### Transport

A Slack app with Incoming Webhooks enabled, one secret webhook URL per
destination channel.

An incoming webhook is bound to the channel it was authorized for, and a payload
cannot redirect it. That property is a security feature here, and the design
leans on it: each allowlisted repository maps to an approved **destination key**,
and only the key resolves to a secret URL. A destination is never derived from
issue or PR content — untrusted text from a public repository must not be able
to influence where a message goes, and cannot name a channel that is not already
in the configuration.

Suggested destination keys: `engineering-updates` and `automation-alerts`. These
are logical routes in configuration, not channels to create or post to without
the workspace owner's authorization.

Default scope: PRs the pipeline owns. All-repository PR notification is a later,
explicit setting.

### Policy

| Event | Message | Delivery rule |
| --- | --- | --- |
| Tracked PR opened | PR opened, draft state, current checks | Immediately, even with checks pending |
| Draft → ready | Ready-for-review intent | Must not imply CI passed |
| Verification passed | Verified, ready for human review | Requires latest-head checks and evidence; **gated off in v1**, see D-011 |
| Run blocks or fails | Reason and required action | On state transition only, never per poll |
| PR merged | Merged, merge SHA, issue reference | Requires GitHub `merged=true` |
| PR closed unmerged | Closed without merge | Distinct from success |

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
2. Create or reuse an approved Slack app, enable Incoming Webhooks.
3. Authorize a webhook per destination.
4. Store URLs as secrets; configure repository → destination routing.
5. Verify formatting against a local fake endpoint first.
6. Send a live test only after the owner authorizes that specific destination.
7. Confirm delivery and error reporting without exposing the URLs.

Steps 5 and 6 are ordered deliberately: formatting bugs are found against the
fake endpoint, not in somebody's channel.

## 10. Schema

```sql
-- Raw inbound deliveries. Kept for replay and debugging, not just dedupe.
CREATE TABLE deliveries (
    delivery_id   TEXT PRIMARY KEY,          -- X-GitHub-Delivery
    event         TEXT NOT NULL,             -- X-GitHub-Event
    action        TEXT,
    repo          TEXT NOT NULL,
    payload       TEXT NOT NULL,             -- raw JSON body
    received_at   TEXT NOT NULL,
    status        TEXT NOT NULL,             -- accepted | ignored | duplicate | error
    reason        TEXT                       -- why ignored or rejected
);
CREATE INDEX idx_deliveries_received ON deliveries(received_at);
CREATE INDEX idx_deliveries_repo_event ON deliveries(repo, event, action);

-- One row per tracked issue.
CREATE TABLE tasks (
    id                 INTEGER PRIMARY KEY,
    repo               TEXT NOT NULL,
    issue_number       INTEGER NOT NULL,
    issue_title        TEXT,
    issue_state        TEXT NOT NULL,        -- open | closed, as last observed
    labels             TEXT NOT NULL,        -- JSON array, last observed
    state              TEXT NOT NULL,        -- see state machine
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_reconciled_at TEXT,
    UNIQUE (repo, issue_number)
);
CREATE INDEX idx_tasks_state ON tasks(state, last_reconciled_at);

-- One row per authorized attempt.
CREATE TABLE runs (
    id                 TEXT PRIMARY KEY,     -- opaque run ID, also the idempotency key
    task_id            INTEGER NOT NULL REFERENCES tasks(id),
    state              TEXT NOT NULL,
    attempt            INTEGER NOT NULL,     -- 1, 2, 3 ... for reruns

    approved_by        TEXT NOT NULL,        -- GitHub login of the approver
    approved_via       TEXT NOT NULL,        -- allowlist | repo_permission
    approved_at        TEXT NOT NULL,

    -- Revocation after start is recorded, not acted on (D-006).
    approval_revoked_by TEXT,
    approval_revoked_at TEXT,

    session_id            TEXT,
    session_url           TEXT,
    session_status        TEXT,              -- provider `status`, verbatim
    session_status_detail TEXT,              -- provider `status_detail`, verbatim
    session_polled_at     TEXT,
    acus_consumed         REAL,
    max_acu_limit         INTEGER,           -- ceiling sent at creation
    structured_output     TEXT,              -- validated JSON from the session

    branch             TEXT,                 -- devin/issue-<n>-<run-id>
    base_sha           TEXT,                 -- baseline revision given to Devin
    pr_number          INTEGER,
    pr_url             TEXT,
    pr_state           TEXT,                 -- open | closed
    pr_draft           INTEGER,              -- 0 | 1
    head_sha           TEXT,                 -- latest observed PR head
    checks_state       TEXT,                 -- pending | passed | failed | unknown
    checks_head_sha    TEXT,                 -- head the check state refers to
    review_state       TEXT,                 -- approved | changes_requested | none
    merged_sha         TEXT,
    extra_pr_urls      TEXT,                 -- JSON array; session opened >1 PR

    env                TEXT NOT NULL,        -- live | sim, never mixed in reports

    lease_owner        TEXT,
    lease_expires_at   TEXT,

    error              TEXT,
    started_at         TEXT,
    ended_at           TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- At most one active run per task. Partial index so terminal runs never collide.
CREATE UNIQUE INDEX idx_runs_one_active ON runs(task_id)
    WHERE state IN ('queued','starting','running','session_blocked','pr_open');

-- One PR maps to at most one run.
CREATE UNIQUE INDEX idx_runs_pr ON runs(pr_number)
    WHERE pr_number IS NOT NULL;

CREATE INDEX idx_runs_claimable ON runs(state, lease_expires_at);

-- Append-only audit of every state change.
CREATE TABLE transitions (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT REFERENCES runs(id),
    task_id       INTEGER NOT NULL REFERENCES tasks(id),
    from_state    TEXT,
    to_state      TEXT NOT NULL,
    cause         TEXT NOT NULL,             -- webhook | poll | reconcile | operator
    delivery_id   TEXT,                      -- when caused by a webhook
    detail        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_transitions_task ON transitions(task_id, created_at);

-- Notifications, committed with the state change that produced them.
CREATE TABLE outbox (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER NOT NULL REFERENCES tasks(id),
    run_id         TEXT REFERENCES runs(id),
    kind           TEXT NOT NULL,            -- pr_opened | ready_for_review | verified
                                             -- | needs_human | pr_merged | pr_closed
                                             -- needs_human carries a reason:
                                             -- blocked | no_output | expired | failed
                                             -- | capacity | acu_limit | scope
                                             -- | approval_revoked
    -- Fingerprint over (task, kind, destination, relevant revision/state).
    -- A new head SHA yields a new fingerprint; an unchanged poll does not.
    fingerprint    TEXT NOT NULL UNIQUE,
    destination    TEXT NOT NULL,            -- approved destination key, never from issue text
    channel        TEXT NOT NULL,
    payload        TEXT NOT NULL,            -- rendered message JSON
    state          TEXT NOT NULL,            -- pending | sent | dead
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_response  TEXT,                     -- Slack response, never the URL
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    sent_at        TEXT
);
CREATE INDEX idx_outbox_pending ON outbox(state, next_attempt_at);
```

`dedupe_key` is the reason a task cannot be announced twice: the insert is part
of the same transaction as the transition, and a second attempt violates the
unique constraint and is discarded.

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
  `checks_state` back to pending for the new `head_sha`. Verification is always
  a statement about one specific revision.
- **Fetch current GitHub state before acting on a delayed event.** A late
  `pull_request` delivery must not move a merged PR back to open. This is the
  rule that makes out-of-order delivery survivable.
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
7. Reconciliation loop, including tag-based orphan recovery.
8. Report endpoint.

Steps 1–4 spend nothing, touch nobody's channel and cover the majority of the
logic. That is the point of the simulated adapter being a first-class component
rather than a test helper.

## 15. Deliberately out of scope for v1

- Automatic merge. Never in v1.
- Acting on review comments automatically.
- Check/status evaluation to derive "review-ready". Notify on PR opened and PR
  closed; treat everything between as the reviewer's business. Required checks,
  re-runs, in-progress suites and forks each have enough edge cases to be their
  own milestone.
- Slack interactive actions, message updates, Events API subscription.
- Follow-up instructions to an existing session from a reviewer.
- Similar-bug discovery, feature implementation, deployment verification.

Threading is the one later-release item worth designing for now: see
`decisions.md`, D-009.
