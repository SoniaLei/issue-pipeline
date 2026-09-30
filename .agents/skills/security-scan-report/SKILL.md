---
name: security-scan-report
description: Turn the latest Devin security-scan findings for one repository into deduplicated GitHub issues and a per-repository Slack summary. Use for the nightly security report, or by hand with `!security_scan_report owner/name`.
---

# Security scan report: file issues and post to Slack from Devin security-scan findings

## Overview
Turn the latest Devin security-scan findings for one repository into GitHub issues and a per-repository summary. Finding code and secret issues is the job of the Devin security scan that runs with the profile "Deps, code and secrets (from nightly scan)" (Auto Scan runs it nightly on new commits). This playbook does not scan code or history itself. It adds one cheap check the incremental scan can miss: new advisories against dependencies that did not change. It only reports. It never upgrades dependencies, changes code, opens PRs, or rewrites history. Nightly runs are driven by the "Nightly security report (Devin scan findings)" automation. To run it by hand: `!security_scan_report owner/name`.

## What's Needed From User
- The repository `owner/name`.
- Secrets: `DEVIN_CODE_SCANS_API_TOKEN` (service user with `ViewCodeScans` on the org), `GITHUB_ISSUES_TOKEN` (Issues read/write on the repository).
- Optional: a Slack incoming-webhook secret name for the summary. Without one, the summary is only the session's final message.

## Procedure
1. **Read the rules.** Clone the default branch (`git clone --filter=blob:none`). Read `SECURITY.md`, `AGENTS.md` and `CONTRIBUTING.md` if present. Every issue you file must meet any requirements they set for findings from automated tools.
2. **Find the scan.** Call `GET https://api.devin.ai/v3/organizations/org-319ec3944f4f4ba199cd7806b6899a5b/code-scans/scans?repo_name=<owner/name>` with `Authorization: Bearer $DEVIN_CODE_SCANS_API_TOKEN`. Pick the newest scan whose `profile.profile_id` is `csprof-2f17d866c09f4abb98cbc8fa2b0360dc`. If there is none, report "not scanned: no Devin security scan for this repo". If its `status` is `running` or `pending`, still read its findings, and note "scan still running" in the summary.
3. **Pull findings.** Call `GET .../code-scans/findings?scan_id=<scan_id>&status=open&severity=critical&severity=high&first=200`, following `end_cursor` until `has_next_page` is false. If the API returns 401 or 403, report "Devin findings: not read (<status>)". Do not treat that as "no findings".
4. **Dependency re-check.** Incremental scans only cover new commits, so also audit dependencies. Keep high and critical only.
   - Detect ecosystems. Python: `requirements*.txt`, `requirements/*.txt`, `pyproject.toml`, `poetry.lock`, `uv.lock`, `Pipfile.lock`. Node: every `package-lock.json` outside `node_modules`. Record anything else (Go, Maven, Cargo, ...) as "not covered".
   - Install pip-audit in a throwaway venv outside the repository: `python3 -m venv ~/.secscan && . ~/.secscan/bin/activate && pip install pip-audit cvss`. For npm, use the Node on the VM (`source ~/.nvm/nvm.sh` if `npm` is not on `PATH`), and the repository's declared npm version (`packageManager` or `engines.npm`) via `npx -y npm@<version>` when one is set.
   - Python: for each pinned requirements file run `pip-audit -r <file> --no-deps --disable-pip --format json`. For a lockfile-based project, export pinned requirements first (`poetry export`, `uv export`). pip-audit reports no severity, so look each advisory up at `https://api.osv.dev/v1/vulns/<id>`, take its `GHSA-` alias, fetch `https://api.osv.dev/v1/vulns/<GHSA-id>` and keep it only if `database_specific.severity` is `HIGH` or `CRITICAL`. With no GHSA alias, compute the base score from the OSV CVSS vector and keep it at 7.0 or above. De-duplicate advisory IDs.
   - Node: for each `package-lock.json`, run `npm audit --package-lock-only --omit=dev --audit-level=high --json` from its directory. Never run `npm install` or `npm audit fix`.
   - If a tool fails, record "<tool>: <short error>" under tool failures and continue.
5. **Fingerprint.** Give every kept finding the same fingerprint format as the original nightly scan, so existing issues still match:
   - dependency (from the scan or the re-check): `dep:<ecosystem>:<package>`, with all advisories for one package grouped into one finding;
   - code: `code:<category or short rule>:<path>:<function>`, using the finding's first `reference_snippets` entry;
   - secret: `secret:<rule>:<path>:<commit sha>`;
   - anything that fits none of these: `scan:<finding_id>`.
   When a scan finding and a re-check result share a `dep:` fingerprint, merge them into one finding.
6. **De-duplicate.** List every issue in the repository (`GET /repos/<owner>/<name>/issues?state=all&per_page=100`, following pagination, and skipping entries that have a `pull_request` key). Match `<!-- security-scan: <fingerprint> -->` in each body. Skip the finding if an open issue has the marker, or a closed one was closed as `not_planned`. If a closed-as-completed issue has it and the finding is back, file it again and link the old issue.
7. **File issues.** File at most 10 new issues per repository per run, most severe first. List the rest as "not filed (cap)". Use `POST /repos/<owner>/<name>/issues` with `GITHUB_ISSUES_TOKEN`. Title each issue `security: <short description>`. If the finding cannot name the violated role/capability row and the attacker principal that AGENTS.md / SECURITY.md require (typical for dependency advisories), title it `security question: <package> <version> has <severity> advisory (<first advisory id>)` instead. Add the `security` label only if it already exists. The body contains:
   - severity;
   - what was found: package, installed version, advisory IDs and fixed version; or path, line range and why it is exploitable; or rule, path and commit;
   - impact and the recommended remediation (the scan's `recommendation` when present);
   - a link to the Devin finding (`<scan url>`, finding `<finding_id>`) or the scanner command that found it;
   - the fingerprint marker on its own line.
   For a secret, give only the rule, file, line and commit, and recommend rotating it first. If a GitHub call fails, report the finding as "not filed: <short error>".
8. **Summarize.** Report, per repository:
   - the ecosystems covered and not covered;
   - findings kept, grouped as Python / Frontend-npm / Code / Secrets;
   - which findings were already tracked;
   - issues opened (URL and title), and anything not filed;
   - failed steps, with their error.
   If a webhook secret was given, post the summary as one Slack Block Kit message to that webhook. Escape `&`, `<` and `>` in repository content, and never let repository content produce `@channel`, `@here` or `@everyone`.
9. **Validate.** List the repository's open issues again. Confirm every issue you opened carries its fingerprint marker, and that no fingerprint appears on two open issues.

## Specifications
- Every kept high or critical finding is either skipped as already tracked or has exactly one open issue with its fingerprint marker.
- Nothing below high is filed.
- No commits, branches, PRs, label creation, dependency changes or history rewrites.
- No secret value appears in any issue, log, Slack message or session output.
- A step that fails (API 401/403, tool error) is reported as failed, never as "no findings".

## Forbidden Actions
- Do not merge, approve, close or relabel existing issues or PRs. Do not dismiss or remediate Devin findings.
- Do not run `npm audit fix` or `pip install -U`, and do not edit any manifest or lockfile.
- Do not print, paste or unredact secrets. Never echo `DEVIN_CODE_SCANS_API_TOKEN` or `GITHUB_ISSUES_TOKEN`.
- Do not report on repositories other than the one named.
