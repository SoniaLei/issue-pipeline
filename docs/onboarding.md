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

# Onboarding

A reading and doing path for an engineer joining this repository, or for an
agent asked to change it. It takes about an hour, spends no ACUs and needs no
credentials until the last optional step.

## 1. Know what you are looking at (10 min)

The service turns an authorized GitHub issue into a Devin session, correlates
the PR the session opens, records what GitHub says about that PR's **current
head** (checks, Devin Review, human review, merge), notifies Slack, and shows
all of it on a dashboard. It is a hosted service that receives webhooks; it is
not a Devin Automation. Nothing in it merges, deploys or acts on review
comments.

Read, in this order:

1. The generated [DeepWiki](https://deepwiki.com/SoniaLei/issue-pipeline) —
   *Overview*, *Task lifecycle*, *Authority boundaries*. Diagrams and source
   links; the quickest picture of what is built. It is generated from the
   code and is **not** the record of intent.
2. [`README.md`](../README.md) — running, configuring and reading the
   dashboard; the outcome definitions table is the vocabulary everyone uses.
3. [`architecture.md`](architecture.md) §1–4 (components, domain model, state
   machine, stages), then §8b (review gate), §12 (human review and merge) and
   §16 (documentation and the sweep).
4. [`decisions.md`](decisions.md) — skim the titles; read fully whichever
   decision governs the thing you are about to change. Each says what the
   choice is *not* and when to revisit it. Recent: D-030 (verified = current
   head), D-031 (skills in the target repo), D-032 (analytics beside GitHub),
   D-033 (review gate), D-034 (wiki vs docs), D-035 (overnight sweep).

If the wiki and the docs disagree, the docs state intent, the wiki states what
was built, and the gap is a bug in one of them: open an issue.

## 2. Run it without spending anything (15 min)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest && ruff check . && ruff format --check . && mypy app scripts
python scripts/run_simulation.py --serve
```

Open <http://127.0.0.1:8000/dashboard>. You are on the **Simulation** page;
the **Live** page is empty by construction. Click the task row: the timeline
shows every durable event of the simulated run — label, session, PR, check
suites, Devin Review request → clear verdict, verification, human approval,
merge — and the *Devin Review* and *Devin's account* cards show provider
progress separately from GitHub's verdict and Devin's cost figures separately
from GitHub's outcome.

Things to notice, because they are the design in miniature:

- *Checks passed*, *review clear* and *verified* are three different numbers.
- Every card that shows something Devin said is labelled *provider*; every
  outcome count is GitHub's.
- The header's *data as of* is the newest write in the store, not the page
  render time.

## 3. Make a change (20 min)

- Branch, change, add or adjust a test under `tests/`, run the four commands
  above. `pre-commit install` runs the same on each commit; CI runs them plus
  the simulated end-to-end run.
- If behaviour changes, update the relevant section of `architecture.md`. If
  a *why* changes, add a decision (`D-0nn`) rather than editing an old one out
  of existence — supersede it and say so.
- If you add a page-worthy area, add it to `.devin/wiki.json` so the wiki
  follows the architecture rather than the directory tree (≤30 pages).
- Open a PR. It gets CI, a Devin Review (findings are a conversation on the
  PR, not a verdict on you) and a human review. A maintainer merges.

Conventions that matter here: one `env` (`live`/`sim`) per figure, never a
total; nothing read from Devin drives a state transition; a new PR head resets
every verification fact; secrets come from the environment, never from issue
or PR content; `git add .` is not used in this repository (stage what you
mean).

## 4. Where sessions get their runtime knowledge

When the pipeline starts a Devin session for a **target** repository, the
session reads that repository's `.agents/skills/<name>/SKILL.md` for how to
stand it up and test it (D-031). The first is
[`superset-local-runtime-testing`](https://github.com/SoniaLei/superset-cognition-demo/tree/master/.agents/skills)
on superset-cognition-demo. Improve a skill by an ordinary PR in that
repository; the pipeline itself stays repository-agnostic.

## 5. Ask Devin

Ask Devin (Devin app, or the public wiki page) answers questions about this
repository grounded in the wiki and the code — "where is the maintainer gate
enforced?", "what resets verification?", "what does *awaiting verdict*
mean?". Agents can read the same material through the DeepWiki MCP
(`read_wiki_structure`, `read_wiki_contents`, `ask_question`). Asking creates
nothing: no issue, no PR, no session.

## 6. The overnight sweep

A scheduled Devin Automation runs nightly against this repository, compares
code with README/architecture/decisions, looks for defects it can prove with
a test, and opens an issue or a small PR for each actionable finding, then
posts one Slack line. You will see its PRs and issues in the morning. Treat
them as proposals from a colleague who has read everything and merged nothing:
review, accept, amend or close with a reason. It never merges (D-035).
Extending it to other repositories is proposed in D-037 and waiting on Q-022.

## 7. Optional: run it live

Needs a public endpoint, a repository webhook on the watched repository, a
Devin service-user token and Slack — an incoming webhook for top-level posts,
or a bot token for one post per PR with its history threaded and reacted
under it (D-036) — see *Running it live* in the README. `scripts/run_live.sh --tunnel` proves the path from a laptop.
A live run spends real ACUs up to `MAX_ACU_LIMIT` per session.
