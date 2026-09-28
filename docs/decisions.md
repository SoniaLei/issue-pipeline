# Decisions

Each entry records what was decided, why, and what would make us revisit it.
Status is `accepted`, `proposed` (needs a call from the maintainer) or
`superseded`.

---

## D-001 — Approval is authorized by actor, not by label presence

**Status**: accepted

On a public repository, anyone granted triage rights — and any bot — can apply a
label. Since `devin-ready` authorizes paid work, treating the label as the
authorization makes the spend control only as strong as the loosest permission
in the repository.

The `sender` on the label event is checked against an explicit maintainer
allowlist, or against `write` / `maintain` / `admin` repository permission,
depending on configured policy. The approving login and timestamp are stored on
the run. An issue opened with the label already applied goes through the same
check on its author. Issue comments never authorize anything.

**Mechanism for v1**: an explicit maintainer allowlist (maintainer decision,
Q-002). Repository-permission checking is written behind the same interface but
left disabled, so switching is configuration rather than a rewrite.

**Planned uplift**: move to `write` / `maintain` / `admin` permission checking
once editing the allowlist becomes friction. See the revisit register in
`open-questions.md`.

**Revisit if** the repository moves to a model where triage rights are granted
as sparingly as write access.

---

## D-002 — Task, run and session are separate entities

**Status**: accepted

A task is the issue we are tracking, a run is one authorized attempt, a session
is Devin's execution of that attempt. Collapsing them means a rerun either
destroys the history of the first attempt or requires a parallel history table.

Keeping them separate also gives the active-run constraint a natural home: the
uniqueness is over runs in active states, not over tasks, so a task can
accumulate attempts without conflict.

**Revisit if** reruns are dropped from scope entirely.

---

## D-003 — Idempotency is structural, not procedural

**Status**: accepted

GitHub redelivers webhooks, and reconciliation intentionally re-derives state
that webhooks may already have applied. Relying on "check before write" leaves a
race between the check and the write.

Instead: dedupe on `X-GitHub-Delivery`; one active run per task enforced by a
partial unique index; one PR per run enforced by a unique index; one
notification per event enforced by `outbox.dedupe_key`. Every one of these is a
database constraint, so a duplicate is a failed insert rather than a second
Devin session or a second Slack message.

Transitions are also idempotent at the application level: re-applying an event
to a task already in the destination state succeeds as a no-op.

**Revisit** never; this is the load-bearing property of the whole design.

---

## D-004 — Partial unique index for the active-run constraint

**Status**: accepted

A plain `UNIQUE (task_id)` on runs would prevent a task from ever having a
second attempt. The constraint we actually want is "at most one run in an active
state per task", which is a partial unique index over the active state list.
SQLite supports partial indexes, and so does PostgreSQL, so this survives the
eventual migration.

The cost is that the active-state list appears in the schema and must be kept in
step with the state machine. A test asserts the index predicate matches the
application's set of active states.

---

## D-005 — Unhappy session outcomes are explicit states

**Status**: accepted, status vocabulary superseded by D-026

A Devin session can end waiting on a human, suspended, or complete with no pull
request. These are not rare. A design whose only path out of
`running` is "a PR appears" leaves those tasks live forever with nobody told.

`session_blocked`, `expired` and `no_output` are therefore first-class states,
each with a grace period and each producing a `needs_human` notification that
carries the session URL. D-026 gives the concrete v3 status mapping that reaches
them. The session URL is the actionable part of that message:
a blocked session is resolved by a human opening it and replying.

---

## D-006 — Cancellation is pre-execution only; a running session is left to finish

**Status**: accepted (maintainer decision, Q-004)

Removing the label or closing the issue **before** the session starts moves the
task to `cancelled` and nothing is spent.

Once a session is running, revocation does **not** stop it. A session is usually
close to done by the time anyone reacts, and the cost already incurred is sunk
either way, so killing it discards the work without recovering the spend. The
revocation is instead recorded on the run (`approval_revoked_by`,
`approval_revoked_at`) and a `needs_human` notification is sent saying approval
was withdrawn mid-run and the session is being allowed to finish. If it produces
a PR, the PR is correlated and announced as normal, carrying the revocation note
so the reviewer sees it before merging.

The practical consequence is that the only hard spend ceiling on a started run
is `max_acu_limit` (D-015, D-018), not the label. That makes Q-005 more
load-bearing than it would otherwise be. D-018 also establishes that there is no
documented stop endpoint, so "let it finish" is close to the only implementable
policy in any case.

Stopping our polling would stop our observation, not the work and not the spend,
so "cancel" is never implemented as "stop polling".

**Revisit if** a run is ever allowed to be expensive enough that abandoning it
mid-flight is cheaper than completing it.

---

## D-007 — PR correlation converges from two sources, first writer wins

**Status**: accepted, amended by D-019

The `pull_request.opened` webhook and the Devin session's own `pull_request`
field both reveal the PR. The webhook is usually first; the poll covers a missed
delivery. Rather than choosing one, both write through the same correlation
function, and the unique index on `runs.pr_number` makes the second one a no-op.

Correlation matches on the run's recorded head branch or on the PR body closing
the tracked issue, with the repository check and the multi-PR rule added by
D-019.

---

## D-008 — Outbox pattern for all notifications

**Status**: accepted

A Slack post inside the transaction that changes state would roll back a
committed decision on a network error, or commit the state change and lose the
message. The outbox row is written in the same transaction as the transition and
delivered afterwards by the worker with retries and a dead-letter state.

This also gives notification delivery its own observable history, which is what
makes "why was I not told about PR 412" answerable.

---

## D-009 — Design the Slack message model as threaded from the start

**Status**: accepted — confirmed by the incoming-webhook constraint

Threading was originally a later-release item. Retrofitting it means rewriting
every call site, because a flat message needs only a channel while a threaded
message needs the parent `ts` persisted against the task and threaded through
every send.

The compromise: v1 stores a nullable `thread_ts` on the task and every outbox
row carries the task, so the transport *can* thread. Incoming webhooks cannot
return a message `ts`, so v1 ships flat, and switching to the Slack Web API
later turns threading on without touching `notifications.py`'s call sites.

Confirmed independently: an incoming webhook does not return the message
timestamp that threading requires, so threading is not merely deferred by
choice — it is unavailable until a Web API bot adapter exists. Persisting
`thread_ts` now costs one nullable column and removes the rewrite later.

**Revisit** when the Web API token is available; that is the moment threading
becomes free.

---

## D-010 — Simulated Devin adapter is a first-class component

**Status**: accepted

`devin_client.py` exposes one interface with two implementations, live and
simulated, the latter driven by `fixtures/simulated_session_events.json`. The
whole state machine, outbox and reconciliation loop can be proven against
fixtures at zero ACU cost, including the unhappy paths from D-005 which are
awkward to provoke against the live API on demand.

The simulated adapter is therefore part of the shipped application, selected by
configuration, not a test double living under `tests/`.

---

## D-017 — Target the v3 organization API; keep v1 only as a reference

**Status**: accepted

**Correction to an earlier claim in this document's review**: v3 is the current
API, not a proposal. Verified endpoints:

| Operation | Endpoint |
| --- | --- |
| Create | `POST /v3/organizations/{org_id}/sessions` |
| Get | `GET /v3/organizations/{org_id}/sessions/{devin_id}` |
| List | `GET /v3/organizations/{org_id}/sessions` |
| Message | `POST /v3/organizations/{org_id}/sessions/{devin_id}/messages` |
| Tags | `POST /v3/organizations/{org_id}/sessions/{devin_id}/tags` |
| Archive | `POST /v3/organizations/{org_id}/sessions/{devin_id}/archive` |
| Delete | `DELETE /v3/organizations/{org_id}/sessions/{devin_id}` |

v3 matters beyond the path change: it returns `status` plus `status_detail`,
`pull_requests[]` with `pr_state`, `acus_consumed`, and a populated
`structured_output` — none of which v1 gives usefully (`structured_output` is
typed `null` there). Several earlier decisions in this document were written
against the v1 shape and are amended accordingly: D-005 (status mapping), D-007
(correlation), D-015 (ACU ceiling).

Authentication is a service user or PAT with `UseDevinSessions`
(`org.devins.use`) at organization level. A **service user**, so the pipeline
does not die with an individual's account.

---

## D-018 — There is no stop endpoint; `max_acu_limit` is the spend control

**Status**: accepted

The documented surface has no endpoint that cancels a running session. Archive
and delete are record operations; a message asking the session to stop is
cooperative, not guaranteed; an application timeout stops only our tracking.

Therefore `max_acu_limit`, set on the create call, is the only enforcement that
holds, and it must be set on every run. Polling `acus_consumed` and reacting is
not an equivalent: by the time a poll observes an overrun there is nothing
reliable to invoke. Polling continues for reporting and for tuning the ceiling.

This also retroactively supports D-006 — "let a revoked run finish" is not only
the cheaper policy, it is close to the only implementable one.

**Revisit if** a stop or cancel endpoint is documented.

---

## D-019 — Correlate on identity established before the PR exists

**Status**: accepted, amends D-007

The branch name `devin/issue-<n>-<run-id>`, the body marker
`<!-- automation-run: <run-id> -->` and the session tags `run:<run-id>` /
`repo:<owner>/<name>` are all fixed at creation, so correlation is a lookup.

A PR attaches to a run only on: allowlisted repository for that run, **and**
(head branch matches **or** body carries the marker), **and** the run is in a
state that can accept a PR. A title match never suffices, and neither does a
marker alone — on a public repository the marker is visible text that anyone can
paste into their own PR body, so it authenticates nothing by itself.

The session tags are the part that is ours rather than the model's: a prompt
instruction can be misread, a tag set through the API cannot.

`pull_requests` is an array, so a session can open several PRs. The first
correlated one is primary and drives the state machine; further ones are
recorded and surfaced but do not fork the lifecycle.

---

## D-020 — The standing task contract lives in a playbook, not the prompt

**Status**: accepted

"Read the contributing guide, reproduce or explain why not, add a failing
regression test, focused change on a task branch, run the checks, open a PR with
cause and evidence, leave the merge alone" is invariant across every run. Put it
in a playbook referenced by `playbook_id`.

The prompt then carries only the variable part: repository, issue snapshot,
baseline revision, run ID, branch name, PR marker, acceptance criteria, allowed
scope. The contract is versioned in one place, and the difference between two
prompts is the actual task rather than a wall of repeated boilerplate.

---

## D-021 — Pass an explicit `secret_ids` list

**Status**: accepted

Omitting `secret_ids` gives the session *all* organization secrets. The design
already states that issue content is task data and not permission to touch
secrets or access controls; passing an explicit minimal list — empty where the
repository needs nothing — is the version of that statement the API enforces.

Prompt instructions are a request. Not supplying the credential is a control.

---

## D-022 — Use structured output for the session's self-report, never for verification

**Status**: accepted

v3 accepts a `structured_output_schema` and returns validated
`structured_output`, so the session's account of itself (reproduced yes/no,
branch, regression test, tests run, outcome) arrives as typed JSON instead of
prose to be parsed.

Its one irreplaceable use is the *reproduction blocked* path, which produces no
PR, no branch and no GitHub-observable evidence of any kind. Without structured
output that outcome is indistinguishable from a session that simply achieved
nothing.

It is never treated as verification, and never as correlation: a session
reporting `"outcome": "fixed"` is a claim. GitHub is the evidence.

---

## D-023 — Destination keys, never destinations from content

**Status**: accepted

An incoming webhook posts only to the channel it was authorized for, which is a
useful constraint rather than a limitation. Each allowlisted repository maps to
an approved destination key; the key resolves to a secret URL. Nothing derived
from issue, PR or branch text participates in routing.

Related and equally load-bearing on a public repository: all user-controlled
text is escaped and `@channel` / `@here` / `<!everyone>` broadcasts are
neutralized before rendering. An issue title is a stranger's input, and an
unescaped one is a channel-wide ping from that stranger.

---

## D-024 — Notification suppression is by fingerprint including the revision

**Status**: accepted, refines D-008

The outbox `dedupe_key` becomes a fingerprint over `(task, event kind,
destination, relevant revision or state)`. Including the revision is what lets
suppression be aggressive without going deaf: the same check result on the same
head SHA is one message no matter how many times it is polled, while the same
result on a *new* head SHA is genuinely new and gets through.

No per-commit and no per-poll messages.

---

## D-025 — Ambiguous session creation is resolved by tag lookup, never retried

**Status**: accepted

A timed-out create call may or may not have started a paid session. Retrying
blind risks two sessions on one issue, which the active-run constraint cannot
prevent because it is about our rows, not the provider's.

Resolution is to list sessions filtered by the `run:<run-id>` tag: the session
either exists and is adopted, or does not and creation is safe. This is the
reason tags are set at creation rather than appended afterwards — a tag appended
after the fact is absent in exactly the failure case that needs it.

If the listing is itself unavailable, the run goes to `failed` with reason
`uncertain_create` for human inspection. Never a second create.

---

## D-026 — Provider status is stored verbatim, beside our state, never merged

**Status**: accepted, refines D-005

v3 reports `status` (`new`, `claimed`, `running`, `exit`, `error`, `suspended`,
`resuming`) and `status_detail` (`working`, `waiting_for_user`,
`waiting_for_approval`, `finished`, plus suspension reasons). Both are persisted
verbatim next to our own state, never merged into one column, so a disagreement
between the provider's view and ours is visible rather than lost.

Two mapping traps worth naming:

- **`finished` is a `status_detail` under `running`, not a terminal `status`.**
  A mapping that reads only `status` never observes completion.
- **Suspension for `out_of_credits`, `out_of_quota`, `org_usage_limit_exceeded`
  or `contract_expired` is not a task failure.** It maps to `failed` with reason
  `capacity` and an operator-directed message. Announcing "the fix failed" when
  the organization is out of credits sends a maintainer to review a diff that
  does not exist.

---

## D-011 — Check and status evaluation is out of v1

**Status**: accepted

Deriving "review-ready" from checks means handling which checks are required,
re-runs, in-progress suites, and pull requests from forks. Each is a source of
wrong notifications, and wrong notifications train people to ignore the channel.

v1 notifies on PR opened, needs-human, merged and closed-unmerged. What happens
between opened and closed is the reviewer's business. Check-derived review-ready
detection is a later milestone once the surrounding machinery is stable.

**Revisit** after the pipeline has run on real issues for long enough to know
which checks matter.

---

## D-012 — SQLite now, PostgreSQL at the multi-host boundary

**Status**: accepted

One API process and one worker on a shared volume, with WAL mode and a busy
timeout, is well inside SQLite's comfortable range, and it keeps local
development and CI to a single file. The store module confines SQL so the
migration is contained.

**Revisit** before running more than one host, or when write concurrency rises
materially. Partial unique indexes and the outbox pattern both port unchanged.

---

## D-013 — Leases rather than locks for worker claims

**Status**: accepted

A crashed worker holding a lock blocks a run permanently. A lease expires. The
worker writes `lease_owner` and `lease_expires_at`, refreshes the lease on every
poll, and re-checks ownership before every write, so a reclaimed run cannot be
written by the original worker after the fact.

---

## D-014 — Raw deliveries are retained, not just deduplicated

**Status**: accepted

A `deliveries` table storing the raw body with a retention window costs little
and makes replay and post-hoc debugging possible; `scripts/replay_webhook.py`
depends on it. A bare dedupe set would answer "have I seen this?" but not "what
did it say?", which is the question actually asked during an incident.

---

## D-015 — Budget limits are enforced before session creation

**Status**: accepted; one number outstanding. Mechanism settled by D-018.

Three limits, checked in the transaction that moves a run out of `queued`:

| Limit | Value | Enforcement |
| --- | --- | --- |
| Concurrent active runs per repository | **2** (maintainer decision, Q-005) | run stays `queued` with a recorded reason |
| Session starts per repository per rolling day | 10 (default, unconfirmed) | run stays `queued`, carried over to the next day |
| ACU per run | **20 to start, then measured** (D-029) | `max_acu_limit` on the create call |

A capped run is **queued, never dropped**: a silently discarded run is
indistinguishable from a bug to the maintainer who applied the label, and the
authorization it carries is still valid tomorrow.

The ACU ceiling is the odd one out. The first two are ours to enforce and
reversible; the third must be set at creation and cannot be changed afterwards
(D-018), so it is the one number that genuinely blocks the create call.

---

## D-016 — "Sufficiently specified" is the maintainer's judgement, not a check

**Status**: accepted for v1 (maintainer decision, Q-001) — **revisit planned**

The intake description required an issue to be "sufficiently specified" before
starting. As written that is a judgement call in the middle of an otherwise
precise authorization path, and an unspecifiable gate becomes either a rubber
stamp or an inconsistent one.

For v1 the check is dropped: applying `devin-ready` *is* the maintainer
asserting the issue is specified enough. Eligibility is therefore mechanical —
repository allowlisted, issue open, label present, actor authorized.

**Planned uplift**: require named issue-template sections (repro steps,
expected, actual) to be present and non-empty, and decline approval with a
comment when they are not. See the revisit register in `open-questions.md`.

**Revisit when** issue volume is high enough that under-specified issues are
wasting sessions — the signal is `no_output` and `session_blocked` outcomes
traceable to thin issue bodies.

---

## D-027 — GitHub App, with no permission to merge

**Status**: accepted (maintainer decision, Q-006)

A GitHub App rather than a repository webhook plus a PAT. Per-installation
tokens, a clean permissions boundary, higher rate limits, and no dependency on
one person's account for a service that authorizes spend. It also makes the R-2
uplift to repository-permission checking a configuration change rather than a
credential change, since an installation token can already read collaborator
permission.

Requested permissions, least-privilege for v1:

| Scope | Access | Why |
| --- | --- | --- |
| Issues | Read & write | intake and label state; write only if the issue comment in Q-020 lands |
| Pull requests | Read | correlation, draft state, merge state |
| Contents | Read | baseline revision and setup files |
| Checks / Commit statuses | Read | unused in v1, needed when D-011 lands |
| Metadata | Read | mandatory |

No Contents write and no merge permission. D-005 says the pipeline must not
merge; this makes it *unable* to, which is the version that survives a bad
prompt, a confused reconciliation path or a future contributor who thinks
auto-merge would be convenient. The branch and PR are pushed under Devin's own
GitHub authentication, not the App's, so the App never needs write access to
code.

The App's webhook secret and installation key are the two credentials that
authorize the whole pipeline; they are environment secrets and are never
logged, per D-023's rule on notification content.

**Revisit if** the pipeline is ever asked to push commits itself — which would
be a different design, not a permission change.

---

## D-028 — Devin is called as a dedicated service user

**Status**: accepted (maintainer decision, Q-014)

The pipeline authenticates as a service user holding `UseDevinSessions`
(`org.devins.use`) and `ViewOrgConsumption`, not as an individual's personal
token. An unattended service keyed to a person's account fails whenever that
person's access changes, and the failure surfaces as sessions that silently
stop being created — exactly the class of fault this design spends effort
avoiding elsewhere.

This is also the identity that appears on every session, so attribution in
consumption analytics separates pipeline spend from human spend without any
extra bookkeeping.

The token is an environment secret, never logged. Build-order steps 1–4 run
against the simulated adapter and require no Devin credential at all, so
provisioning does not gate the start of implementation.

---

## D-029 — The ACU ceiling starts loose and is set by measurement

**Status**: accepted (maintainer decision, Q-015); starting value provisional

The documentation defines what an ACU is but gives no mapping from ACUs to a
unit of work, so there is no number that can be derived in advance. It has to
be measured. What can be decided in advance is which direction to be wrong in.

The error is asymmetric: a ceiling set too high wastes the difference on a
runaway session, but a ceiling set too low severs legitimate work mid-fix — and
**that run still costs the full ceiling while producing nothing mergeable**.
Paying for work and discarding it is strictly worse than paying somewhat too
much for work that lands. So the opening value is deliberately loose.

**Starting value: 20 ACU per run**, explicitly a placeholder.

The tightening procedure is the actual decision:

1. `acus_consumed` is recorded on every run from the first, including
   `no_output`, `session_blocked` and `failed` runs — the failures are the
   informative tail, and they are the runs a ceiling is for.
2. After roughly 20 completed runs, the ceiling is set to about 2× the p90 of
   runs that produced a merged PR.
3. It is re-examined whenever repository setup or test duration changes
   materially, since setup is spend incurred before any useful work begins.

A run that hits the ceiling becomes `failed` with reason `acu_limit` and
notifies `needs_human`. It is **not** auto-retried: an identical retry under an
identical ceiling fails identically, at the same price.

**Revisit when** step 2's data exists — this decision is designed to be
superseded, and a ceiling still at 20 after fifty runs means the measurement
loop was never closed.

## D-030 — Observability reports one environment at a time, and "verified" means the current head

**Decision.** The dashboard (`/dashboard`, `/api/dashboard`,
`/api/tasks/{id}/timeline`) reads only the store. It is filtered to exactly
one `env` (`live` or `sim`) per request, with no aggregate view. A run is
*verified* only when every check suite GitHub reported for the PR's **current**
head completed successfully; a new head resets the answer to unknown until new
suites arrive. Check evaluation still drives no state transition (D-011) — the
"awaiting review" bucket is derived at report time from `pr_open` plus that
verification fact.

**Why one env.** A simulated merge is a fixture replayed against an in-memory
database; counting it beside a live one would report success that never
happened. The page therefore has no "all" mode, names the environment in a
banner and shows how many runs of each kind the store holds, so an empty live
page is legible as "nothing ran", not "nothing recorded".

**Why current head.** Check results are keyed by head SHA (`checks` table) and
folded per run into `checks_state` + `checks_head_sha`. `is_verified` requires
`checks_head_sha == head_sha`. Without that, a passing suite for commit A would
continue to vouch for a PR whose head had moved to commit B — the stale
verification problem in one line. A late suite for a superseded head is
recorded and ignored.

**What is evidence and what is testimony.** A session's structured output
("tests added", "tests run") is displayed as the session's account in the
timeline and task table, and never counted as verification. Only GitHub's check
suites do. The state-change, session, PR, check, verification, review and
Slack rows in the timeline are durable (`run_events`, `outbox`) and written at
the moment they happen, not reconstructed.

**What "verified" is not yet.** It is "all suites GitHub reported", not "the
suites branch protection requires". Narrowing it means reading branch
protection through the App and is a later uplift; until then a repository with
an optional, flaky suite will under-report verification, which is the safe
direction to be wrong in.

**Freshness.** `data_as_of` is the newest write across runs, deliveries,
outbox and heartbeats — a store-side fact. `generated_at` is when the report
was built. The worker heartbeat is separate again, so a stale worker is visible
even while the API keeps answering.

**Revisit when** the service is hosted and has live runs: the metrics only
describe runs this service tracked, so Automation-started sessions are not in
them by construction.

---

## D-031 — Repository runtime knowledge lives in skills committed to the target repository

**Status**: accepted (maintainer decision)

**Decision.** How to stand up and test a *target* repository — interpreter and
toolchain versions, isolated config, which hosts a dependency install reaches,
fixture and permission gotchas — is captured as a skill file committed to that
repository under `.agents/skills/<name>/SKILL.md`. The first one is
`superset-local-runtime-testing` on `SoniaLei/superset-cognition-demo`
(https://github.com/SoniaLei/superset-cognition-demo/pull/5), written from the
run that verified PR #3 there. Sessions started by this pipeline read it from
the checkout as a quick starter for reproduction and regression testing.

**Why the target repository, not the service.** The pipeline is
repository-agnostic: the prompt (D-020) carries the task, the playbook carries
the invariant contract, and neither should know that Superset wants Python 3.11,
npm 11 and `cdn.sheetjs.com` allowlisted. That knowledge changes with the
target's code, so it is versioned, reviewed and merged next to that code by the
people who own it. Adding a second repository to `REPO_ALLOWLIST` means adding
a skill there, with no change here.

**Why a file, not the prompt.** The live run on issue #2 lost most of its time
to environment setup, not to the fix. A skill turns that into a read at the start
of the next session instead of a rediscovery, and each run can improve it via an
ordinary PR that a maintainer reviews like any other doc change.

**What it is not.** A skill is guidance, not a gate: it does not authorize
work, grant network access or replace the checks. Network allowlisting for the
hosts it names is still configured on the session/automation side.

**Revisit when** more than one target repository is tracked, to decide whether
a shared skill format or a pipeline-side index of per-repo skills is worth it.

---

## D-032 — Devin analytics are provider testimony: shown beside GitHub's facts, never in place of them

**Decision.** The worker reads Devin's organization analytics — per-session
insights and daily consumption, and organization session/PR counts scoped to
the pipeline's service user — into `session_insights` and `provider_metrics`,
keyed by `env`. The dashboard shows them as a **Cost & efficiency** layer, as
*Devin's account* in each run's timeline, and as a **provider cross-check** in
Integration health. Nothing read from these endpoints drives a state
transition, a notification, or an outcome count. PR opened, verified and merged
stay GitHub's; a disagreement between Devin's counts and the pipeline's is shown
as *drift* with the delta, and is not reconciled by either side.

**Why beside, not instead.** The provider's PR count is its view of what its
sessions created, which is not the same question as "does a pull request exist
on GitHub for this run". Both can be right and still differ — a session opened a
PR the service never correlated, a session was started outside the pipeline
under the same service user, a PR was counted at a different lifecycle point.
Folding the two into one number hides exactly the failures the dashboard exists
to show. The rule is the one already applied to structured output (D-022) and
provider status (D-026): the provider's account is stored verbatim and labelled
as such.

**Why three ACU figures and a source label.** The poll's `acus_consumed` is a
running snapshot; the insights total is the provider's post-hoc figure; daily
consumption is billing-grade and lands at Pacific midnight, often hours after
the session finished. In the live run on issue #6 the consumption endpoint was
empty for a session that had been working for many hours. Treating an absent
figure as zero would make ACUs-per-PR fall over time as sessions finish and
their costs have not been published yet — the opposite of the truth. So the
dashboard prefers billing, then insights, then poll, names which one it is
showing, counts unpriced sessions under *awaiting cost*, and leaves every ratio
as unknown until at least one session is priced.

**Why the org counts are scoped by observed service users.** `metrics/prs` and
`metrics/sessions` cover the whole organization unless filtered. Filtering by
the service-user IDs that sessions *reported* — not a configured ID — means the
cross-check can never accidentally count a human's sessions, and is simply not
attempted until the pipeline has seen at least one of its own.

**Why the worker reads it, one session per tick.** The analytics endpoints are
rate-limited and the webhook path performs no external calls. Reading them
after the run's own work each tick keeps analytics behind pipeline progress and
behind Slack delivery; a failure is recorded on the row and logged, never raised
into the loop. A run keeps being re-read for a settle window after its terminal
state so late billing and analysis can land, then stops.

**What it is not.** Not a spend control — `max_acu_limit` remains the only one
(D-018). Not verification — a session's size or analysis says nothing about
whether the fix is right. Not a feedback loop yet: the action items and skill
usage are displayed as a backlog for D-031 skills, and no agent acts on them.

**Revisit when** live runs have settled through a few billing days, to confirm
the consumption lag and whether `billing` supersedes `session` totals in
practice; and when a separate review agent (deferred) needs these figures as
input.
