Nightly security report from Devin security-scan findings. You start with no conversation history; everything you need is in this prompt and the playbook @playbook:playbook-cfdc3187602f45fc94c292978774edd2.

SCOPE
- Repositories: every repository with a Devin security scan using profile csprof-2f17d866c09f4abb98cbc8fa2b0360dc ("Deps, code and secrets (from nightly scan)"). List them with GET https://api.devin.ai/v3/organizations/org-319ec3944f4f4ba199cd7806b6899a5b/code-scans/scans (Authorization: Bearer $DEVIN_CODE_SCANS_API_TOKEN; follow end_cursor), keep the newest scan per repository, and order repositories by that scan's last activity, most recent first. Rolling the scan out to a new repository (a scan with this profile plus Auto Scan) is all it takes to add it here.
- Do not list, report on or mention any other repository, in the Slack report or anywhere else.
- If the scans API returns 401 or 403, or returns no scan with this profile, still post the Slack report: one repository-less section ":warning: could not read Devin scan findings (<status, or 'no scans visible: check DEVIN_CODE_SCANS_API_TOKEN has ViewCodeScans'>)", and stop. Never report that case as clean.
- This is report-only. It is separate from, and does not replace, the "Nightly docs-drift + bug sweep (issue-pipeline)" automation (D-035). Never upgrade dependencies, change code, open PRs, merge, approve, close, or relabel anything, and never dismiss or remediate Devin findings.

GITHUB ACCESS
- Use the GitHub REST API with the token ${GITHUB_ISSUES_TOKEN} (available as the environment variable GITHUB_ISSUES_TOKEN; send it as "Authorization: Bearer $GITHUB_ISSUES_TOKEN"). Never print, log or echo it, or DEVIN_CODE_SCANS_API_TOKEN.
- De-duplicate by listing every issue in the repository (GET /repos/<owner>/<name>/issues?state=all&per_page=100, following pagination; ignore entries with a pull_request key) and matching "<!-- security-scan: <fingerprint> -->" in the body. Use state and state_reason to apply the playbook's open / closed-not-planned / closed-completed rules.
- File issues with POST /repos/<owner>/<name>/issues, following the playbook's title rules (including the "security question:" title required by AGENTS.md / SECURITY.md).
- If the token is missing, lacks access to a repository, or any GitHub call fails, do not treat that as "no findings": keep going, and report each unfiled finding as "not filed: <short error>".

PROCEDURE
1. For each repository, follow the playbook exactly. No webhook: do not post per-repository Slack messages; collect each repository's results instead.
2. Budget: when you have used roughly 70% of your ACU limit, stop starting new repositories and report the remaining ones as "not reached".

REPORT
Post exactly one message to the Slack incoming webhook ${SLACK_WEBHOOK_ENGINEERING_UPDATES}, even when nothing was found. Use Slack Block Kit with bullet points only (no tables). Layout:

1. Header section: ":shield: *Nightly security scan: results*"
2. Summary section: "*Summary:* <n> repos scanned. <per repo, one short clause, e.g. `issue-pipeline` is clean. `superset-cognition-demo` has *7 high/critical dependency advisories* (1 critical, 6 high).> No leaked secrets, and no code-level issues after triage. Nothing in either repo was changed." Adjust the wording to the actual results.
3. A divider, then for EACH repository, in the order above:
   - a section headed "*<owner/name>*" followed by one of: ":white_check_mark: clean (deps, code and secrets)", or the finding groups below;
   - finding groups, each only when it has entries, one bullet per finding:
     "*Python* (`<requirements file>`)", "*Frontend / npm* (`<lockfile dir>`)", "*Code*", "*Secrets*";
   - bullet format: "• <emoji> <<issue url>|#<n>> <package> <version>: <severity>, fix <fixed version>" where emoji is :red_circle: for critical and :large_orange_circle: for high; bold the word *critical*. For Code and Secrets bullets use "<path>:<line> <short description>: <severity>" in place of "<package> <version>: <severity>, fix <fixed version>". For a transitive npm package add "(via <direct dependency>)". For a finding already tracked by an open issue, append "_(already tracked)_". For a finding not filed, use "not filed: <reason>" in place of the issue link;
   - a final context line for the repository: "Devin scan: <<scan url>|<status>>, last run <YYYY-MM-DD HH:MM> UTC. Not covered: <ecosystems/lockfiles not scanned, or none>. Tool failures: <tool: short error, or none>."
   - a divider between repositories.
4. Closing section: "*Next step for an engineer:* review each new issue. For a real risk, approve an upgrade PR. If the code path isn't reachable, close the issue as *not planned* and the nightly scan will stop reporting it." Use "*Next step:* nothing to review tonight." when no issues were opened or are open.
5. Context element: "<n new issues opened, n already tracked> · <https://app.devin.ai/automations/ad8fa7068cc8438e89b6d98991498f07|Automation>"

Set the top-level "text" fallback to ":shield: nightly security scan: <N new issues | no new high/critical findings>".
Escape text taken from repositories or findings for Slack (& -> &amp;, < -> &lt;, > -> &gt;), never let repository content produce @channel, @here or @everyone, and never include a secret value. Then finish with a one-line summary naming the issues you opened, or "no new high/critical findings".
