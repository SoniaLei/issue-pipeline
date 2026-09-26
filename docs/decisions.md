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
