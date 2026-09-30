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
notification per event enforced by the unique `outbox.fingerprint` (D-024).
Every one of these is a database constraint, so a duplicate is a failed insert
rather than a second Devin session or a second Slack message.

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

**Status**: accepted, amended by D-019. The single shared function below was
never built; see *As built*.

The `pull_request.opened` webhook and the Devin session's own `pull_request`
field both reveal the PR. The webhook is usually first; the poll covers a missed
delivery. Rather than choosing one, both write through the same correlation
function, and the unique index on `runs.pr_number` makes the second one a no-op.

Correlation matches on the run's recorded head branch or on the PR body closing
the tracked issue, with the repository check and the multi-PR rule added by
D-019.

**As built** (checked against the code on 2026-09-29). The two sources take
different routes. GitHub is authoritative and the poll is testimony:

- **Webhook.** `Intake._correlate` applies all three D-019 conditions:
  repository, branch or marker, and eligibility. Only this path writes
  `pr_number` and `head_sha` and queues "PR opened".
- **Poll.** `Worker._record_snapshot` trusts the session it created for this
  run. When Devin reports `pull_requests[]`, the run moves to `pr_open` and
  records `pr_url`, plus any extras (Q-018), without a repository or branch
  check.
- **After either one.** GitHub events own the run (`GITHUB_OWNED` in
  `app/worker.py`), and polls only update cost and output.
- **Why the missing check is safe.** Nothing reported only by the poll can
  become *verified*: checks and Devin Review bind to `head_sha`, which only
  GitHub supplies (D-030, D-033).
- **The unique index** is `runs_one_pr` on `(task_id, pr_number)`, not on
  `runs.pr_number`.

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

**Status**: superseded in part by D-030, D-033 and D-036. Checks are now
evaluated, and the results are announced:

- *Verified* means every suite on the current head passed, plus a clear Devin
  Review where the gate requires one (D-030, D-033).
- Failed checks, findings and verification are announced once per head, in the
  PR's thread (D-036).

What still stands: checks drive no state transition. `awaiting_review` is
unreachable, and "awaiting review" is derived at report time (D-030).

*The original v1 decision follows, kept for history. Its notification list
is replaced by D-036.*

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
| Checks / Commit statuses | Read | current-head check suites for verification (D-030, D-033) |
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

---

## D-033 — Devin Review is a per-head PR gate; repository-wide scanning is a separate, periodic sweep

**Status**: accepted (maintainer decision)

**Decision.** A second, independent reading of every pipeline PR is done by
Devin Review, requested and read through the organization API
(`POST`/`GET /v3/organizations/{org}/pr-reviews`) for the PR's **exact current
head**. It is a stage after `pr_open`, not a second session per PR. With
`REVIEW_GATE_MODE=required` (default) a run is *verified* only when GitHub's
check suites pass on the current head **and** the Devin Review of that same
commit is clear; `advisory` shows the review without withholding verification;
`off` makes no review calls. Findings, provider errors and a review that never
happened are all "not clear". Repository-wide security or architecture scans
(Code Scans / Security Swarm) are a separate periodic activity whose findings
enter as *issues* through the normal `devin-ready` gate; they are not attached
to a PR and never call `remediate` directly.

**Two sources, kept apart.** The `pr-reviews` API says how far a review has
got (`pending`, `running`, `completed`, `errored`, `cancelled`, `skipped`); it
does not say what was found. The verdict is the review the Devin bot
(`devin-ai-integration[bot]`) submits on GitHub for that commit ("found N
potential issues" / "no issues found"), plus its inline comments, which carry a
kind (`bug`, `security`, `flag`). The store keeps both on one row per
`(run, head_sha)`: `status`/`status_at`/`attempts`/`last_error` from the API,
`findings`/`findings_by_kind`/`review_url`/`verdict_at` from GitHub. A
`completed` status with no GitHub verdict is *awaiting verdict*, not clear;
a GitHub verdict with no API status is a verdict all the same. The row is
never re-polled once a verdict exists, so a later API answer cannot overwrite
what GitHub said.

**Why the exact head, and why old rows stay.** A review of commit A says
nothing about commit B. When GitHub reports a new head the run's old review
row is left in place for the audit trail, a `review_gate superseded` event is
written, and a new row is started (and a new request made) for the new head.
Verification reads only the row whose `head_sha` equals the run's current
head. If the API reports that Devin reviewed a *newer* commit than the run
knows, that is recorded as the run's head being skipped — the pipeline waits
for GitHub to tell it about the new head rather than trusting the provider's
view of the PR.

**Why it is not another session.** Devin Review is the tool built for this,
costs XS–S per head, lands its result on the PR where the human reviewer
already looks, and cannot be steered by the PR's own content the way a prompt
can. A bespoke "security reviewer" session per PR would double the ACU spend
per PR and produce testimony (D-022) rather than a GitHub-bound review.

**Why the worker asks, on idle ticks, at most once per head.** Devin Review
also auto-runs on connected repositories, so the request is idempotent by
`(pr_url, head_sha)`: the worker asks once, then only reads. Reads are paced
by `REVIEW_POLL_SECONDS` and capped by `REVIEW_MAX_ATTEMPTS` consecutive
provider errors, after which the head is marked `unavailable` and surfaces in
*Needs attention* with the reason and the action (re-run the review, or fix
the provider/credential problem). Review calls run only when no run advanced
that tick, so a slow provider never delays intake, session polling or Slack.
A failure is stored on that head's row and logged; it neither stops the worker
nor touches any other run.

**What stays GitHub's.** Whether a PR exists, its current head, its checks,
the human review state and the merge are read from GitHub events as before.
The gate adds one more GitHub-bound fact (the bot's review) and one provider
progress indicator; it removes nothing from the human reviewer. Human review
and merge remain mandatory and are shown separately from the bot's verdict
(§12). A run with findings is not failed: it stays `pr_open`, unverified, with
the finding count and kinds in the task row and timeline, and the follow-up —
fix in the same PR, dismiss with a reason on GitHub, or open a follow-up issue
— is a human decision. Nothing merges automatically, including any remediation
PR a scan might propose.

**Revisit when** GitHub's branch protection can be read through the App
(D-030), so "checks passed" and "review clear" can both be narrowed to what
protection actually requires; and if the review API begins to expose findings
directly, in which case GitHub's review remains the verdict and the API's copy
becomes a cross-check (D-032).

---

## D-034 — DeepWiki is generated navigation and context; README and `docs/` remain the canonical statement of intent

**Status**: accepted (maintainer decision)

**Decision.** Both repositories (`SoniaLei/issue-pipeline` and
`SoniaLei/superset-cognition-demo`) are indexed by Devin's DeepWiki, steered by
a committed `.devin/wiki.json` that names the pages the wiki should have and
what each is for. The wiki is the *description* of the code as it is:
architecture diagrams, per-area pages, source links, and the context that Ask
Devin, Devin Desktop and pipeline sessions read. It is not where decisions are
made. `README.md` is the canonical entry point and operating guide,
`docs/architecture.md` the intended design, `docs/decisions.md` the record of
why, and `.agents/skills/` in a target repository the runtime instructions a
session follows (D-031). When the wiki and the docs disagree, the docs state
what was intended, the wiki what was built, and the gap is a bug in one or the
other to be fixed by a reviewed PR.

**Why steer it with a committed file.** An unsteered wiki organises the code
by directory. `wiki.json` lets the page tree follow the architecture instead —
lifecycle, authority boundaries, security, operations, onboarding — and is
versioned and reviewed like any other doc, so a change to the intended
structure of the system is a diff someone approves.

**Why the wiki is not canonical.** It is regenerated from the code; it cannot
hold an intent the code does not yet implement, a rejected alternative, or a
"revisit when". Those live in the decisions log. Making generated text the
source of truth would let an implementation drift redefine the design.

**What it is for.** Onboarding (read the wiki's overview and lifecycle pages,
then the README to run it, then the decisions when changing behaviour); Ask
Devin questions about how something works; and context for Devin sessions,
which read the wiki alongside the checkout. Reading the wiki creates nothing:
no issue, no PR, no session. A change proposal — from a human or from the
scheduled sweep in D-035 — is always a PR reviewed and merged by a maintainer.

**Revisit when** the wiki's refresh behaviour on push is confirmed from the
settings UI, and when a second target repository is added (whether one shared
page template is enough).

---

## D-035 — An overnight scheduled session looks for drift and bugs; it proposes, humans dispose

**Status**: accepted (maintainer decision)

**Decision.** One scheduled Devin Automation runs overnight against
`SoniaLei/issue-pipeline`. It reads the current code, `docs/architecture.md`,
`docs/decisions.md`, the README and the DeepWiki pages, and looks for two
things: places where the implementation and the documented intent disagree,
and defects it can demonstrate (a failing test it can write, a reproducible
misbehaviour). For each actionable finding it opens either a GitHub issue
(when the fix needs a decision or is not small) or a PR with a test (when it is
small and clearly right). It posts a short summary to Slack. It runs at most
once per night, does not start if the previous run is still going, is capped in
ACUs, and never merges anything.

**Why a scheduled session, not a per-push hook.** Drift between docs and code
is slow and cross-cutting; it is cheaper and less noisy to read the whole
picture once a night than to react to each commit. Per-PR concerns are already
covered by CI and the review gate (D-033).

**Why issues *and* PRs, and never a merge.** The sweep's output is a proposal.
A PR from it goes through the same checks, the same Devin Review and the same
human review as any pipeline PR; an issue from it goes through the same
`devin-ready` gate as any other request before any further spend. The sweep
therefore adds no new trust path and cannot change `main` on its own.

**Bounding it.** Fixed prompt in the Automation; explicit repository scope;
one run per schedule with concurrency 1; ACU cap per run; "no actionable
finding" is a valid and expected result and produces only the Slack line. It
must not reopen an issue or PR it already raised for the same finding, and must
link the doc section and code location for every claim.

**What it is not.** Not a security scan (that is Code Scans, separately
authorised); not a substitute for the review gate; not self-healing — nothing
it produces takes effect without a human merging it.

**Revisit when** a month of runs shows the signal-to-noise ratio: widen scope
to the target repository, or narrow the prompt, based on what maintainers
actually acted on.

---

## D-036 — The "PR opened" post is the run's Slack anchor; the rest of the PR's life is a thread and reactions on it

**Status**: accepted (maintainer decision)

**Decision.** With a bot token (`SLACK_TRANSPORT=bot`) the worker keeps the
channel and `ts` that `chat.postMessage` returns for a run's *PR opened*
message, and posts every later message for that run and destination — checks
failed, Devin Review findings, verified, human review, needs-human, merged,
closed — as a reply in that thread, adding one reaction to the anchor per
event. Incoming webhooks stay supported (`SLACK_TRANSPORT=webhook`, the
default) with the previous top-level behaviour, because a webhook returns no
message id and cannot thread or react.

**Why.** A channel that gets one line per lifecycle event per PR is a channel
people mute. One line per PR, whose reactions read as its history at a glance
(:red_circle: checks failed, :mag: findings, :large_green_circle: verified,
:white_check_mark: merged, :no_entry_sign: closed unmerged), keeps the channel readable while the thread keeps
the record.

**What this changes about D-011.** Check-derived messages were kept out of v1
because they were the likeliest source of noise. In a thread the noise cost is
near zero and the signal (the head failed; the head is verified) is what a
reviewer waits for, so failed checks, review findings and verification are
announced — once per head, on the first derivation, never per suite or per
poll, and never for a superseded head.

**Rules.** A follow-up whose anchor is still retrying waits in the outbox for
it. A follow-up with no usable anchor (webhook transport, anchor failed for
good, history from before anchors) goes top-level rather than being dropped.
The reaction is attempted once after the reply is recorded sent; its failure is
stored on that row and not retried; `already_reacted` is success. Fingerprint
suppression is unchanged.

**Authority.** Slack still holds no state that GitHub does not. The anchor is
read only to decide where a message goes; nothing about a run, its
verification or the dashboard's facts depends on it, and losing it costs only
message placement. The bot token gets `chat:write` and `reactions:write` and
nothing else; channel ids come from configuration, never from issue or PR
content (the same rule that governed webhook destinations).

**Revisit when** message updates (`chat.update` of the anchor's text) would
say something reactions cannot, or when a destination needs more than one
anchor per run (e.g. a per-repository digest).

---

## D-037 — The overnight sweep scales as one shared skill, a manifest per repository and a thin Automation per repository

**Status**: proposed (needs a call from the maintainer; see Q-022)

**Context.** D-035 runs one sweep, against this repository, with the whole
procedure written into the Automation's prompt. The same service already
serves more than one repository at runtime (`REPO_ALLOWLIST`, and the
dashboard's `?repo=` filter), and `SoniaLei/superset-cognition-demo` is the
next candidate for a sweep. Copying the prompt per repository would fork the
procedure: every repository would drift from the others and every fix would
have to be made N times.

**Proposal.** Split the sweep into four layers, each versioned and reviewed as
code:

1. **Procedure: one skill.** The steps in D-035 (read the intent docs, run the
   baseline, compare docs with code, skip what is tracked, open an issue or a
   small PR with a failing-then-passing test, never merge, post one Slack
   summary) live in a single `nightly-sweep` skill in a platform repository,
   installed once as an organization plugin. Changing the procedure is one PR,
   and every repository picks it up.
2. **Intent: a manifest in each target repository.** A file such as
   `.devin/sweep.yaml` states what counts as that repository's intent and how
   far the sweep may reach:

   ```yaml
   intent_docs: [README.md, docs/architecture.md, docs/decisions.md]
   scope: [app/, scripts/]            # or named areas in a large codebase
   baseline:
     - pytest -q
     - ruff check .
   label: sweep
   budget: {max_findings: 5, acu_cap: 20}
   slack: engineering-updates
   ```

   The repository's owners own this file, the same way D-031 puts runtime
   knowledge in the target repository.
3. **Trigger: a thin Automation per repository.** Its prompt only names the
   repository and says to run the `nightly-sweep` skill with that repository's
   manifest. Schedule, concurrency 1, ACU cap and network allowlist are
   Automation settings, not prompt text. The PR-lifecycle → Slack companion
   Automation is created alongside it.
4. **Registry: a list of onboarded repositories.** A `repos.yaml` in the
   platform repository lists them. A script or CI job creates or updates each
   repository's Automations from it through Devin's automation interface, so
   the list is the source of truth. The exact endpoints are to be confirmed
   when this is built.

Onboarding a repository then becomes: add it to the registry, commit its
manifest, and save its environment blueprint.

**What does not change.** Everything D-035 bounds stays bound, per repository:
explicit scope, one run per schedule, an ACU cap, "no actionable finding" as a
valid result, no merges, no self-applied `devin-ready`, and nothing taking
effect without a human merging it. The sweep adds no trust path, in any
repository.

**Why not one Automation that loops over every repository.** A single run
would share one ACU cap and one failure domain across repositories, and one
noisy repository would crowd out the others. Keeping one Automation per
repository keeps each budget, schedule and Slack summary separate, while the
skill keeps the procedure the same everywhere.

**Large repositories.** A repository the size of Superset cannot be swept end
to end in one night. Its manifest has to name `scope` areas, and its
`baseline` has to be scoped too (changed-file lint and area-specific tests),
using the environment blueprint's toolchain.

**Revisit when** a second repository has run the sweep for a month. Keep the
layering if its findings were acted on at a rate similar to this repository's.
Narrow its manifest if they were not.

---

## D-038 — Which issue events carry an approval decision

**Status**: accepted (maintainer decision on issue #10)

D-001 says the actor is checked, not the label. That still leaves the question
of *which* events are read as that actor deciding to spend. Issue #10 showed the
risk of getting it wrong: if any later event on a labelled issue re-runs the
gate, an untrusted `devin-ready` is adopted as approval by the next unrelated
maintainer action — an edit, a `bug` label, a reopen.

The events that authorize a run on an issue carrying `devin-ready` are:

- `labeled` with `devin-ready` — the sender put the label there.
- `opened` with the label already present — the author opened it that way.
- `reopened` — the sender chose to bring this issue back into play, label and
  all. A closed issue's run was cancelled or finished; reopening it is a
  decision about this issue, so an allowlisted maintainer's reopen re-approves
  without having to remove and re-apply the label.

`edited`, `unlabeled` of another label, and `labeled` with any other label are
not decisions about spend and never authorize, whoever sends them. A reopen by
someone outside the allowlist is recorded like a drive-by `opened` and starts
nothing.

**Why reopen counts.** The maintainer's expectation was the natural one: "I
reopened it, it still says `devin-ready`, so Devin should pick it back up."
Requiring a remove-and-re-apply of the label to express that is a hidden rule.
The sender is still checked, so the untrusted-label case in issue #10 stays
closed: an outsider's reopen does not spend, and a maintainer's reopen is the
maintainer's own act.

**Revisit if** reopen is used for bookkeeping unrelated to wanting work done
(a triage bot that reopens stale issues, say). Then reopen should drop back to
"awaiting approval" and the label must be re-applied.

---

## D-039 — The nightly security scan runs on Devin security scans; its configuration is kept here

**Status**: accepted (maintainer decision, 2026-09-30)

**Context.** The first nightly security scan was one Automation whose session
ran pip-audit, bandit and gitleaks itself, filed issues and posted to Slack.
Devin security scans now cover the scanning part natively, with a reusable
profile, incremental "scan new commits" runs and tracked findings. What
security scans do not do is file GitHub issues with our fingerprint markers,
skip ones that are already filed, or post the Slack report.

**Decision.** Split the work into three parts:

1. **Find: Devin security scans.** One scan per repository with the org-wide
   profile "Deps, code and secrets (from nightly scan)". A 03:23 London
   Automation runs `scan_new_commits` on each scan.
2. **Report: a reporter session at 05:23 London.** It reads each scan's open
   high and critical findings through the Code Scans API. It re-audits the
   dependencies, because an incremental scan does not see a new advisory
   against a lockfile that hasn't changed. It files issues using the same
   `<!-- security-scan: ... -->` fingerprints as before, so issues that
   already exist still match. It posts one Slack message in the same Block
   Kit layout.
3. **Configuration as code in this repository** (`devin/security-scan/`,
   `.agents/skills/security-scan-report/`). Terraform manages the playbook and
   both Automations. The profile has no Terraform resource or write API, so
   `profile.json` is a snapshot, and `scripts/check_scan_profile.py` reports
   drift.

The reporter finds its repositories through the profile. To add a repository,
start a scan with the profile and add the scan ID to `main.tf`. The same
layering as D-037 applies: one procedure, per-repository scope, and a thin
schedule.

**What does not change.** It is report-only: no remediation, no dependency
upgrades, and no closing or relabelling of issues. Findings enter through
GitHub issues and go through the `devin-ready` gate like any other request
(architecture §8, "Repository-wide scans are not this"). Secrets are
referenced by name and never committed.

**Supersedes** the original "Nightly security scan (authorized repos)"
Automation, which is now disabled.

---

## D-040 — Issue deliveries are ordered by GitHub's snapshot, not by arrival

**Status**: accepted (closes the finding Devin Review raised on PR #28)

GitHub does not promise to deliver webhooks in the order the events happened.
D-038 made a maintainer's `reopened` re-approve a labelled issue, which sharpened
an existing hole: if the `closed` that preceded that reopen arrives *after* it,
the intake cancels the run the reopen just queued, and the issue is open with
no work on it. The mirror case exists for labels — `unlabeled` overtaken by the
`labeled` it undid would approve an issue whose label is already gone.

The intended safety net is reconciliation against the GitHub API (§6), which
does not exist yet. Waiting for it leaves a spend-control gap open, so the
intake now uses what every `issues` payload already carries: the issue as it
was when the event fired, with its `updated_at`. The task records the newest
`issue.updated_at` it has applied; a delivery whose snapshot is strictly older
is recorded in `deliveries` as `stale delivery: …` and changes nothing.

Rules, in order of what they protect:

- Strictly older only. Equal timestamps are applied in arrival order: GitHub's
  clock has second resolution, and two events in the same second have no
  better ordering available. Missing timestamps (fixtures, hand-replayed
  payloads) are never stale.
- Only `issues` events. `pull_request` events already cannot move a terminal
  run backwards (§11), and checks and reviews are keyed by head SHA, which is
  its own ordering.
- Recorded, not dropped. A stale delivery keeps its row and reason, like every
  other rejected delivery, so an operator can see that ordering happened.

**What this is not.** It is not reconciliation. A delivery that never arrives
is still never re-fetched, and the worker still starts a session from the
webhook snapshot rather than from a fresh API read (§5). Those need the GitHub
API client, which remains build-order item 7 (§14).

**Revisit if** GitHub ever delivers a snapshot whose `updated_at` moves
backwards for a real state change, or when the API client lands and re-fetch
can replace the comparison.
