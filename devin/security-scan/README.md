# Nightly security scan: Devin configuration

The nightly security scan runs on Devin. Finding issues is done by Devin
security scans, and a report session turns what they find into GitHub issues
and one Slack message. This directory holds everything about that setup that
can be kept as code. The reasons for the split are in `docs/decisions.md`
(D-039).

| What | Where | How it reaches Devin |
|---|---|---|
| Reporting procedure (`!security_scan_report`) | `.agents/skills/security-scan-report/SKILL.md` | Devin loads the skill from the repo. `main.tf` also publishes it as the playbook the reporter uses. |
| 03:23 London: scan new commits for each scan | `main.tf` (`devin_automation.scan_new_commits`) | Terraform |
| 05:23 London: reporter (issues and Slack) | `main.tf` (`devin_automation.security_report`) and `reporter_prompt.md` | Terraform |
| Scan profile "Deps, code and secrets (from nightly scan)" | `profile.json` | By hand in Devin. `scripts/check_scan_profile.py` checks for drift. |

The following stay in Devin and are not kept here: the scans and their
findings, and the secret values (`DEVIN_CODE_SCANS_API_TOKEN`,
`GITHUB_ISSUES_TOKEN`, `SLACK_WEBHOOK_ENGINEERING_UPDATES`). Prompts refer to
secrets by name only.

## Applying changes

```sh
cd devin/security-scan
export DEVIN_TOKEN=...   # a service-user token that can manage playbooks and automations
terraform init
terraform plan
terraform apply
```

The `import` blocks in `main.tf` let the first apply adopt the resources that
already exist, so it does not create duplicates. The provider cannot read back
the JSON-encoded groups of an automation (triggers, actions, limits and so
on), so the first plan shows them as added. Applying writes back the same
values. Keep the state file out of git, since `.gitignore` already covers it.

## Changing the scan profile

The Devin API has no endpoint for editing a profile, so profile changes are
made in Devin. The change goes through an approval card because the profile is
shared across the org. After that, pull the new profile into the repo:

```sh
DEVIN_CODE_SCANS_API_TOKEN=... .venv/bin/python scripts/check_scan_profile.py          # exits 1 on drift
DEVIN_CODE_SCANS_API_TOKEN=... .venv/bin/python scripts/check_scan_profile.py --pull   # refresh profile.json
```

## Adding a repository

1. Start a security scan of the repository with the profile. Use Deep effort
   for its first scan.
2. Once that scan has completed, add its `scan-...` ID to `local.scan_ids` in
   `main.tf` and apply.
3. Give `GITHUB_ISSUES_TOKEN` issue access to the repository.

The reporter finds the new repository through the profile, so its prompt does
not need to change.
