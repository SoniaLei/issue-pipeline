# Open questions

Questions raised while reviewing the proposal that need a decision before, or
shortly after, implementation starts. Each carries a recommendation so that a
non-answer still leaves a defensible default.

Blocking questions must be answered before the code that depends on them is
written. Non-blocking ones have a working default and can be revisited.

**Status** (reviewed against the code on 2026-09-29): no blocking question
remains open.

- **Decided:** Q-001, Q-002, Q-004, Q-005 (concurrency), Q-006, Q-014, Q-015,
  and Q-021 (by D-032).
- **Resolved by API verification:** Q-007.
- **Resolved, recommendation implemented:** Q-003, Q-008, Q-009, Q-010 and
  Q-017.
- **Still open**, each running on a default. The *Current behaviour* line under
  each question says where that default differs from its recommendation:
  - Q-011, Q-012 (tracked in issue #9), Q-013, Q-016, Q-018, Q-019 and Q-020;
  - Q-022, which waits on D-037.

Two decided values are provisional by design and expected to change: the daily
session cap (10, never explicitly confirmed) and the ACU ceiling (20, a
placeholder to be replaced by measurement — see Q-015).

---

## Revisit register

Deliberate v1 simplifications that were accepted *on the basis* that they get
uplifted later. Each names the signal that says it is time.

| # | v1 behaviour | Uplift to | Signal to act |
| --- | --- | --- | --- |
| R-1 | No content check on the issue; the label is the judgement (Q-001 A) | Mechanical check that issue-template sections are present and non-empty (Q-001 B) | `no_output` or `session_blocked` outcomes that trace back to thin issue bodies; or issue volume outgrowing per-issue maintainer attention |
| R-2 | Approval authorized by an explicit maintainer allowlist (Q-002) | Repository `write` / `maintain` / `admin` permission check | Editing the allowlist becomes routine friction, or an authorized maintainer is blocked by a stale list |
| R-3 | `max_acu_limit` of 20, a placeholder (Q-015) | A measured ceiling, ~2× the p90 of runs that produced a merged PR | ~20 completed runs of recorded `acus_consumed`. A ceiling still at 20 after fifty runs means the measurement loop was never closed |

R-1 and R-2 are wired so the uplift is a configuration change plus one module,
not a redesign: eligibility and authorization each sit behind a single
interface with the stricter implementation written and disabled. R-3 is purely
a configuration value, but it only moves if `acus_consumed` is recorded from
the first run — which is why that recording is not optional.

---

## Q-001 — What does "sufficiently specified" mean at approval time? — **DECIDED: Option A**

The spec requires an issue to be open, approved and "sufficiently specified"
before a session starts. The first two are mechanical; the third is not.

- **Option A** — Drop it. The maintainer applying `devin-ready` is making that
  judgement, and that is what the label is for.
- **Option B** — Make it mechanical: require named issue-template sections
  (repro steps, expected, actual) to be present and non-empty, and refuse
  approval with a comment when they are not.

**Decision**: Option A for v1. Eligibility is repository allowlisted, issue
open, label present, actor authorized — nothing about the body.

**Uplift to Option B is planned**: tracked as R-1 in the revisit register.

**Recorded in**: D-016.

---

## Q-002 — Allowlist, repository permission, or both? — **DECIDED: allowlist**

D-001 settles that the *actor* is checked. It does not settle against what.

- An explicit maintainer allowlist is predictable and auditable, and needs
  editing as the team changes.
- A repository permission check (`write` / `maintain` / `admin`) tracks reality
  automatically, and widens the authorized set every time someone is granted
  write access for an unrelated reason.

**Decision**: explicit maintainer allowlist for v1. The permission-check
implementation is written behind the same interface and left disabled, so the
switch is configuration.

**Uplift to permission checking is planned**: tracked as R-2 in the revisit
register.

**Recorded in**: D-001.

---

## Q-003 — What exactly counts as "blocked"? — **RESOLVED: operator conditions route separately**

The conditions are no longer ambiguous: v3's `status_detail` distinguishes them
directly. What remains is a routing choice, not a detection problem.

| Condition | Detected from | Owner |
| --- | --- | --- |
| Awaiting user input | `running` / `waiting_for_user` | maintainer |
| Awaiting action approval | `running` / `waiting_for_approval` | maintainer |
| Complete, no PR after grace | `running` / `finished` + no correlation | maintainer |
| Session errored | `status = error` | operator |
| Out of credits / quota / contract | `suspended` / capacity reasons | **operator, not maintainer** |
| Idle suspension | `suspended` / `inactivity` | maintainer, resumable by message |
| CI failed | check events | author / reviewer |
| Merge conflict | PR mergeable state | author / reviewer |
| Changes requested | review events | author |

**Remaining question**: capacity suspensions (out of credits, quota exhausted,
contract expired) are an organization problem, not an issue problem. Should
they route to a separate operator destination rather than the engineering
channel?

**Recommendation**: yes — `automation-alerts` for operator conditions
(capacity, `error`, `uncertain_create`), `engineering-updates` for everything
maintainer-facing. The routing table already supports it and the split costs
nothing now. v1 still covers only the session-side conditions; the GitHub-side
three arrive with check evaluation (D-011).

**Resolved.** The recommendation is implemented:

- The `capacity`, `session_error` and `uncertain_create` reasons
  (`OPERATOR_REASONS` in `app/notifications.py`) go to `OPERATOR_DESTINATION`
  (default `automation-alerts`), via `Settings.destination_for` in
  `app/config.py`.
- Everything else goes to the repository's destination or
  `engineering-updates`.
- On the GitHub side, failed checks and a human's `changes_requested` review
  are announced in the PR's thread (D-036).

Merge conflicts are still not detected, because the service never reads
`mergeable` state.

---

## Q-004 — Does label removal during execution stop the run? — **DECIDED: let it finish**

- **Stop**: the label is the authorization; withdrawing it withdraws consent,
  and the spend stops immediately.
- **Let it finish**: a session is usually near-complete by the time anyone
  reacts, and killing it wastes what was already spent.

**Decision**: let it finish. Revocation before the session starts still
cancels; once running, it is recorded on the run and announced as a
`needs_human` notification, and the session continues to completion. Any
resulting PR is announced normally with the revocation noted.

**Follow-on**: this removes the `cancelling` state and makes the ACU ceiling
(Q-005) the only hard limit on a started run, which raises that question's
priority.

**Recorded in**: D-006, reversing its earlier position.

---

## Q-005 — What are the budget numbers? — **PARTLY DECIDED: concurrency 2**

- **concurrent active runs per repository — 2 (decided)**
- session starts per repository per rolling day — default 10, not yet confirmed
- ACU ceiling per run — split out to Q-015, still open

With concurrency 2, a run that cannot start is queued rather than rejected, and
the daily cap is what actually bounds spend per day. Running on the default of
10 until told otherwise.

When the daily cap is hit, queued runs **carry over** to the next day rather
than being dropped; a silently dropped run is indistinguishable from a bug to
the maintainer who applied the label.

**Recorded in**: D-015.

---

## Q-006 — GitHub App or repository webhook? — **DECIDED: GitHub App**

Per-installation tokens, a clean permissions boundary, higher rate limits, and
no dependency on one person's PAT. It also makes the R-2 uplift to repository
permission checking a configuration change rather than a credential change,
since an installation token can already read collaborator permission.

Requested permissions, least-privilege for v1:

| Scope | Access | Why |
| --- | --- | --- |
| Issues | Read & write | intake, label state; write only if Q-020 lands |
| Pull requests | Read | correlation, draft/merge state |
| Contents | Read | baseline revision, setup files |
| Checks / Commit statuses | Read | current-head check suites for verification (D-030, D-033) |
| Metadata | Read | mandatory |

No write to Contents and no merge permission — the App must be incapable of
merging, not merely instructed not to (D-005 rationale, applied to credentials).
Devin pushes its branch under its own GitHub authentication, not the App's.

Events subscribed: `issues`, `pull_request`, `pull_request_review`,
`pull_request_review_comment` (Devin Review findings) and `check_suite`
(`SUPPORTED_EVENTS` in `app/intake.py`). The legacy `status` event is not
consumed.

**Recorded in**: D-027.

---

## Q-007 — Which Devin API version? — **RESOLVED: v3**

Verified against the current documentation. v3 is the current API:
`POST /v3/organizations/{org_id}/sessions` and siblings. It returns `status`
plus `status_detail`, `pull_requests[]` with `pr_state`, `acus_consumed` and a
populated `structured_output`, and accepts `max_acu_limit`, `tags`,
`secret_ids`, `playbook_id` and `structured_output_schema` at creation.

An earlier note in this review doubted the v3 path. That doubt was wrong; the
correction is recorded in D-017.

**Consequences**: D-017 (target v3), D-018 (no stop endpoint, `max_acu_limit`
is the control), D-022 (structured output), D-025 (tag-based create recovery),
D-026 (two-dimensional status).

**Residual**: confirm the service user and `UseDevinSessions`
(`org.devins.use`) permission exist before the live adapter is wired — see
Q-014.

---

## Q-008 — What is the grace period before `no_output`? — **RESOLVED: 5 minutes, configurable**

A session can report `finished` marginally before its PR is visible to us.
Declaring `no_output` too eagerly produces a false "needs human" notification;
too slowly delays a real one.

**Recommendation**: 5 minutes of polling after `status_detail = finished` with
no correlated PR, then `no_output`. Note `finished` is a detail under `running`,
not a terminal status — a poll that reads only `status` never reaches this
condition at all.

**Resolved.** Implemented as recommended: `NO_OUTPUT_GRACE_SECONDS`, default
300, is applied by `Worker._grace_verdict` in `app/worker.py`.

---

## Q-009 — One Slack channel or per-repository routing? — **RESOLVED: per-repository map with a default**

The design says "configured Slack channels", plural. Routing per repository is
cheap now (a config map, with the channel stored on the outbox row) and
intrusive later.

**Recommendation**: config map keyed by repository resolving to an approved
destination key, with a default. Already reflected in the schema, and required
rather than optional: a destination must never be derivable from issue text
(D-023). Q-003 adds a second axis — operator conditions to a separate
destination from maintainer conditions.

**Resolved.** Implemented as recommended. `REPO_DESTINATIONS` maps a repository
to a destination key, with `DEFAULT_DESTINATION` as the fallback.
`SLACK_DESTINATIONS` and `SLACK_CHANNELS` resolve a key to a webhook URL or
channel ID, and the chosen key is stored on each outbox row.

---

## Q-010 — Who can trigger a rerun, and how? — **RESOLVED: re-apply the label**

Runs in `no_output`, `expired`, `failed` or `cancelled` are terminal, and a
retry needs a new run ID from an explicit action. The trigger is unspecified.

- Remove and re-apply `devin-ready`, same authorization path, no new surface.
- An operator endpoint on the report page.

**Recommendation**: label re-application for v1. It reuses the authorization
path exactly, and an operator endpoint is a second authorization surface to
secure.

**Resolved.** Implemented as recommended; there is no rerun endpoint. When an
authorized maintainer applies `devin-ready` to a task with no active run,
`Intake._approve` in `app/intake.py` creates a new queued run. Re-applying it
while a run is active only reinstates a withdrawn approval, and never starts a
second run. That reinstatement is covered by
`test_relabel_by_a_maintainer_reinstates_approval_without_a_second_run`. The
new-run-after-a-terminal-run path has no dedicated test yet.

---

## Q-011 — What happens to the issue after merge? *(non-blocking)*

"Issue disposition" appears in the merge stage without definition. Does the
pipeline close the issue, comment on it, or leave it alone?

**Recommendation**: comment with the PR and merge SHA, and leave closing to
GitHub's `Fixes #N` linkage or to a human. Writing to a public issue is a
visible action and should be conservative.

**Current behaviour**: the pipeline never writes to GitHub, so the issue is
left alone. Open, together with Q-020.

---

## Q-012 — Delivery retention window? *(non-blocking)*

`deliveries` stores raw webhook bodies, which contain issue and PR content from
a public repository. Retention has a storage cost and a tidiness cost.

**Recommendation**: 30 days, pruned by the worker.

**Current behaviour**: `DELIVERY_RETENTION_DAYS` (default 30) is read and
`Store.prune_deliveries` exists, but nothing calls it, so deliveries are kept
forever. Tracked in issue #9.

---

## Q-013 — Does the report endpoint need authentication? *(non-blocking)*

The report exposes issue numbers, approver logins, session URLs and ACU spend.
Session URLs in particular should not be public.

**Recommendation**: bind it to localhost, or put it behind a shared secret. Do
not expose it unauthenticated alongside the public webhook endpoint.

**Current behaviour**: not followed. `/report`, `/report.txt`, `/dashboard` and
`/api/*` have no authentication and are served by the same app as `/webhook`.
`scripts/run_live.sh` binds `0.0.0.0`, and with `--tunnel` all of them are
public. This needs a call before the service is hosted.

---

## Q-014 — Devin credential — **DECIDED: dedicated service user**

A dedicated service user, not an individual's PAT: an unattended pipeline that
stops working when one person's access changes is a question of when, not if.

Permissions it needs:

| Permission | For |
| --- | --- |
| `UseDevinSessions` (`org.devins.use`) | create, poll, message sessions |
| `ViewOrgConsumption` | the consumption endpoints below |

Provisioned before the live adapter is wired; the token is an environment
secret. Steps 1–4 of the build order run against the simulated adapter and need
none of it.

**Recorded in**: D-017, D-028.

---

## Q-015 — `max_acu_limit` per run — **DECIDED: start generous, then tighten**

The error here is asymmetric. Too high wastes the difference on a runaway run.
Too low severs legitimate work mid-fix, and that run still costs the full
ceiling while producing nothing mergeable — the worst outcome available, since
it pays for the work and throws it away. So the opening value is deliberately
loose and exists to stop pathological runs, not to trim normal ones.

**Starting value: 20 ACU per run.** This is a judgement, not a
documented figure — the documentation defines what an ACU is but gives no
mapping from ACU to a unit of work, so no defensible number can be derived in
advance. It is a placeholder chosen to be wrong in the safe direction, and its
only real job is to be replaced by measurement.

**Tightening procedure**, which is the substance of this decision:

1. Record `acus_consumed` on every run from the first one, including runs that
   end `no_output`, `session_blocked` or `failed`. The failures are the
   informative tail.
2. After ~20 completed runs, set the ceiling to roughly 2× the p90 of runs that
   produced a merged PR.
3. Re-check whenever the repository's test suite or setup time changes
   materially, since setup is spend before any work happens.

A run that hits the ceiling is `failed` with reason `acu_limit`, notified as
`needs_human`, and is explicitly **not** auto-retried — a retry at the same
ceiling would fail the same way at the same price.

**Recorded in**: D-015, D-029.

---

## Q-016 — Playbook or prompt for the standing task contract? *(non-blocking)*

D-020 puts the invariant requirements in a playbook and keeps only per-run
variables in the prompt. This assumes a bug-remediation playbook is created and
maintained alongside the service.

**Recommendation**: playbook. Confirm who owns it — it is the closest thing the
pipeline has to a quality standard, and it should not be edited casually.

**Current behaviour**: `DEVIN_PLAYBOOK_ID` is optional and is passed when set.
`app/prompts.py` also restates the contract, so a deployment without a
playbook still carries it. Who owns the playbook is still unconfirmed.

---

## Q-017 — Which secrets does a session actually need? — **RESOLVED: explicit list, empty by default**

D-021 requires an explicit `secret_ids` list because omitting it grants all
organization secrets. That needs a per-repository answer.

**Recommendation**: start with an empty list and add only what the repository's
setup genuinely fails without. A public repository's test suite usually needs
nothing.

**Resolved.** Implemented as recommended. `DEVIN_SECRET_IDS` is empty by
default, and the create call always sends `secret_ids`, as an empty list if
nothing is set (`app/devin_client.py`). That list is never omitted, so a session
never inherits every organization secret (D-021). Any additions are per
deployment.

---

## Q-018 — What happens when a session opens more than one PR? *(non-blocking)*

`pull_requests` is an array. D-019 makes the first correlated PR primary and
records the rest.

**Remaining**: should a second PR raise a notification? It usually means the
change outgrew its scope, which a reviewer would want to know.

**Recommendation**: yes, as a `needs_human` with reason `scope`. Cheap, and
scope creep is exactly the thing a review gate exists to catch.

**Current behaviour**: only half done:

- The worker records additional PRs from the session poll in `extra_pr_urls`,
  and the webhook path ignores a second PR for a run that already has one.
- The `scope` reason text exists in `app/notifications.py`, but nothing raises
  it, so no notification is sent.

---

## Q-019 — Retention and visibility of `structured_output`? *(non-blocking)*

It is the session's own account of itself and is stored on the run. It is the
only record of a *reproduction blocked* outcome, which has no GitHub artifact
at all.

**Remaining**: does it appear in the Slack message, or only the report?

**Recommendation**: report only, except `reproduction_note` on a
`not_reproducible` outcome, which is the whole content of that notification.

**Current behaviour**: structured output is stored on the run and shown only in
the report and the dashboard. No Slack message includes it, not even
`reproduction_note`.

---

## Q-020 — Does the pipeline post back to the GitHub issue? *(non-blocking)*

Currently it notifies Slack and stays silent on GitHub. On a public repository
the issue author sees nothing — not that work started, nor that a PR exists.

**Recommendation**: one comment when a run starts and one when a PR opens, both
terse. It sets the reporter's expectation and makes the automation legible to
people outside Slack, which on a public repository is everyone. Deliberately
not a status feed. Related to Q-011 (post-merge comment); if both land, they
should be one consistent voice.

**Current behaviour**: the pipeline makes no GitHub writes. Open.

---

## Q-021 — Which consumption source is authoritative for reporting? — **DECIDED: labelled precedence (D-032)**

Two now exist. `acus_consumed` on the session response is live but reflects the
session's current state; the consumption endpoints are billing-aligned and
day-bucketed at midnight PST:

```
GET /v3/organizations/{org_id}/consumption/daily
GET /v3/organizations/{org_id}/consumption/daily/sessions/{session_id}
```

**Recommendation**: `acus_consumed` for the per-run number in the report and
for the tightening procedure in Q-015 — it is already on the poll and needs no
extra permission beyond the session scope. The daily endpoints back a
pipeline-wide spend view and reconcile against billing. Do not sum
`acus_consumed` across runs and call it the pipeline's spend: the day boundary
is PST, so the two will not agree, and the endpoint is the one that matches the
invoice.

**Decided by D-032**, which refines the recommendation:

- The dashboard shows billing-grade daily consumption when it exists, then the
  insights total, then the poll's `acus_consumed`, and names the source it used.
- Unpriced sessions are counted under *awaiting cost* instead of as zero.
- `acus_consumed` is still recorded on every run for the Q-015 tightening
  procedure.

---

## Q-022 — Taking the overnight sweep to other repositories (D-037) *(non-blocking)*

D-037 proposes a shared skill, a manifest per repository, a thin Automation
per repository and a registry. Before it is accepted:

- **Where the platform lives:** this repository, or a separate platform
  repository that holds the skill, the registry and the sync script.
- **Automation shape:** one Automation per repository (separate budgets and
  failure domains, as proposed), or one parameterised Automation per schedule.
- **Scope for `SoniaLei/superset-cognition-demo`:** which areas the first
  manifest names, or whether it sweeps only code the pipeline has recently
  changed.
- **Registry sync:** manual at first, or a script from day one.

Until then D-035 stands as written: one sweep, this repository only.
