# Devin resources for the nightly security scan: the reporting playbook and
# the two scheduled automations. The scan profile has no Terraform resource;
# see profile.json and scripts/check_scan_profile.py.
#
# Auth: DEVIN_TOKEN must hold a service-user token (cog_...) that can manage
# playbooks and automations in the organization.

terraform {
  required_version = ">= 1.5"
  required_providers {
    devin = {
      source  = "cognitionai/devin"
      version = "~> 0.2.0"
    }
  }
}

provider "devin" {}

locals {
  org_id = "org-319ec3944f4f4ba199cd7806b6899a5b"

  # One scan per repository, all using profile.json. Adding a repository here
  # (after starting its first scan with that profile) adds it to the nightly
  # incremental run; the reporter finds it through the profile.
  scan_ids = [
    "scan-4e91153cdbd449b48a2e0e53b7c32455", # SoniaLei/superset-cognition-demo
    "scan-3a85fcc59cbe4afe8a82aabfe3d8144a", # SoniaLei/issue-pipeline
  ]

  skill_path = "${path.module}/../../.agents/skills/security-scan-report/SKILL.md"

  email_on_dispatch_failure = jsonencode({ email = { when = "dispatch_failed" } })
}

resource "devin_playbook" "security_scan_report" {
  org_id = local.org_id
  title  = "Security scan report: file issues and post to Slack from Devin security-scan findings"
  macro  = "!security_scan_report"
  # The skill file is the source of truth; the playbook is its body without
  # the YAML front matter.
  body = replace(file(local.skill_path), "/^---\\n(?s:.*?)\\n---\\n+/", "")
}

resource "devin_automation" "scan_new_commits" {
  org_id = local.org_id
  name   = "Nightly security scan: new commits (security profile repos)"
  run_as = "organization"
  triggers = jsonencode([{
    event_type = "schedule:recurring"
    conditions = { any = [{ all = [{
      field    = "rrule"
      operator = "recurrence"
      value    = "DTSTART;TZID=Europe/London:19700101T000000\nRRULE:FREQ=DAILY;BYHOUR=3;BYMINUTE=23"
    }] }] }
    replies = []
  }])
  actions = jsonencode([for id in local.scan_ids : { type = "scan_new_commits", scan_id = id }])
  session_settings = jsonencode({
    net_policy = { allow = [{ hostname = "git-manager.devin.ai" }] }
  })
  notifications = local.email_on_dispatch_failure
}

resource "devin_automation" "security_report" {
  org_id = local.org_id
  name   = "Nightly security report (Devin scan findings)"
  run_as = "organization"
  triggers = jsonencode([{
    event_type = "schedule:recurring"
    conditions = { any = [{ all = [{
      field    = "rrule"
      operator = "recurrence"
      value    = "DTSTART;TZID=Europe/London:19700101T000000\nRRULE:FREQ=DAILY;BYHOUR=5;BYMINUTE=23"
    }] }] }
    replies = []
  }])
  actions = jsonencode([{
    type   = "start_session"
    prompt = file("${path.module}/reporter_prompt.md")
    session = {
      tags            = ["security-scan"]
      bypass_approval = false
    }
  }])
  limits = jsonencode({
    max_acu_limit = 15
    invocations   = { max_per_window = 2, window_seconds = 43200 }
  })
  concurrency = jsonencode({ max_concurrent_runs = 1, max_queue_depth = 0 })
  session_settings = jsonencode({
    net_policy = { allow = [for h in [
      "git-manager.devin.ai",
      "api.devin.ai",
      "hooks.slack.com",
      "pypi.org",
      "files.pythonhosted.org",
      "registry.npmjs.org",
      "api.osv.dev",
      "api.github.com",
      "github.com",
      "release-assets.githubusercontent.com",
      "objects.githubusercontent.com",
    ] : { hostname = h }] }
  })
  notifications = local.email_on_dispatch_failure
}

# The resources above already exist; these adopt them on the first apply
# instead of creating duplicates.
import {
  to = devin_playbook.security_scan_report
  id = "org-319ec3944f4f4ba199cd7806b6899a5b/playbook-cfdc3187602f45fc94c292978774edd2"
}

import {
  to = devin_automation.scan_new_commits
  id = "org-319ec3944f4f4ba199cd7806b6899a5b/auto-f43971e18f1b4ac89c38a7e8dba4807d"
}

import {
  to = devin_automation.security_report
  id = "org-319ec3944f4f4ba199cd7806b6899a5b/auto-ad8fa7068cc8438e89b6d98991498f07"
}
