#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Environment configuration and repository policy.

Every value that decides *who may spend money* and *where messages go* is
read from the environment here and nowhere else. Neither is ever derived from
issue or pull request content, which on a public repository is attacker
controlled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_APPROVAL_LABEL = "devin-ready"

# D-015/D-029. The ACU ceiling is a placeholder to be replaced by measurement,
# not a derived figure; it is set loose on purpose because a severed run still
# costs the full ceiling and produces nothing mergeable.
DEFAULT_MAX_ACU = 20
DEFAULT_MAX_CONCURRENT_RUNS = 2
DEFAULT_MAX_DAILY_SESSIONS = 10

# Grace between a session reporting itself finished and the run being called
# `no_output`, to let a PR webhook arrive.
DEFAULT_NO_OUTPUT_GRACE_SECONDS = 300
DEFAULT_RUN_MAX_SECONDS = 4 * 60 * 60
DEFAULT_LEASE_SECONDS = 120

# Devin analytics. Insights are re-read when a run moves and otherwise every
# refresh interval; a finished run keeps being re-read for the settle window
# because billing-grade consumption lands after the session does.
DEFAULT_INSIGHTS_REFRESH_SECONDS = 600
DEFAULT_INSIGHTS_SETTLE_SECONDS = 24 * 60 * 60
DEFAULT_METRICS_REFRESH_SECONDS = 300

# Review gate (D-033). `required`: a PR head is verified only when GitHub
# checks pass *and* Devin Review completed on it with no findings.
# `advisory`: the review is requested and shown but does not gate.
# `off`: no review is requested.
DEFAULT_REVIEW_GATE_MODE = "required"
DEFAULT_REVIEW_POLL_SECONDS = 60
DEFAULT_REVIEW_MAX_ATTEMPTS = 5


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_map(value: str) -> dict[str, str]:
    """Parse ``key=value,key=value`` into a dict."""
    pairs: dict[str, str] = {}
    for item in _split(value):
        if "=" not in item:
            raise ConfigError(f"expected key=value, got {item!r}")
        key, _, val = item.partition("=")
        pairs[key.strip()] = val.strip()
    return pairs


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Validated process configuration."""

    github_webhook_secret: str
    repo_allowlist: frozenset[str]
    maintainer_allowlist: frozenset[str]
    approval_label: str = DEFAULT_APPROVAL_LABEL

    database_path: str = "data/pipeline.db"
    env: str = "sim"

    devin_mode: str = "sim"
    devin_api_base: str = "https://api.devin.ai"
    devin_org_id: str = ""
    devin_api_token: str = ""
    devin_playbook_id: str = ""
    devin_secret_ids: tuple[str, ...] = ()

    slack_mode: str = "fake"
    # webhook: incoming-webhook URLs, top-level messages only.
    # bot: chat.postMessage with a bot token; PR-lifecycle updates thread under
    # and react to the run's "PR opened" post.
    slack_transport: str = "webhook"
    slack_bot_token: str = ""
    # Logical destination key -> secret webhook URL (webhook transport) or
    # channel ID (bot transport). A destination is only ever chosen by key,
    # never by anything a user can write.
    slack_destinations: dict[str, str] = field(default_factory=dict)
    slack_channels: dict[str, str] = field(default_factory=dict)
    repo_destinations: dict[str, str] = field(default_factory=dict)
    default_destination: str = "engineering-updates"
    operator_destination: str = "automation-alerts"

    max_acu_limit: int = DEFAULT_MAX_ACU
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS
    max_daily_sessions: int = DEFAULT_MAX_DAILY_SESSIONS
    no_output_grace_seconds: int = DEFAULT_NO_OUTPUT_GRACE_SECONDS
    run_max_seconds: int = DEFAULT_RUN_MAX_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS

    # Shared operator token for the dashboard, report and API. Required in
    # live mode; unset in sim mode leaves them open for local use.
    dashboard_token: str = ""

    poll_interval_seconds: float = 2.0
    delivery_retention_days: int = 30

    # Devin analytics (Session Insights, consumption, org metrics). Read on the
    # worker's tick, bounded to one session per tick and one metrics call per
    # interval, so a slow analytics endpoint never delays a run.
    analytics_enabled: bool = True
    insights_refresh_seconds: int = DEFAULT_INSIGHTS_REFRESH_SECONDS
    insights_settle_seconds: int = DEFAULT_INSIGHTS_SETTLE_SECONDS
    metrics_refresh_seconds: int = DEFAULT_METRICS_REFRESH_SECONDS

    # Review gate. One pr-reviews call per idle worker tick: a GET for the
    # current head first, a POST only when no review of it exists.
    review_gate_mode: str = DEFAULT_REVIEW_GATE_MODE
    review_poll_seconds: int = DEFAULT_REVIEW_POLL_SECONDS
    review_max_attempts: int = DEFAULT_REVIEW_MAX_ATTEMPTS

    def repo_allowed(self, full_name: str) -> bool:
        return full_name.lower() in self.repo_allowlist

    def actor_authorized(self, login: str) -> bool:
        """Q-002/D-001: an explicit allowlist for v1.

        The repository-permission implementation belongs behind this same call
        so the uplift (R-2) is configuration rather than a redesign.
        """
        return login.lower() in self.maintainer_allowlist

    def destination_for(self, repo: str, operator: bool = False) -> str:
        """Resolve a logical destination key for a repository.

        Operator-owned conditions (capacity, provider errors) route away from
        the engineering channel: an org that is out of credits is not a failed
        bug fix, and telling a maintainer it is sends them to review a diff
        that does not exist.
        """
        if operator:
            return self.operator_destination
        return self.repo_destinations.get(repo.lower(), self.default_destination)

    def webhook_url_for(self, destination: str) -> str:
        try:
            return self.slack_destinations[destination]
        except KeyError as exc:
            raise ConfigError(f"no webhook URL configured for {destination!r}") from exc

    def slack_target_for(self, destination: str) -> str:
        """What the transport posts to for a logical destination."""
        if self.slack_transport == "bot":
            try:
                return self.slack_channels[destination]
            except KeyError as exc:
                raise ConfigError(
                    f"no Slack channel configured for {destination!r}"
                ) from exc
        return self.webhook_url_for(destination)


def _choice(name: str, default: str, allowed: frozenset[str]) -> str:
    value = os.environ.get(name, default).lower()
    if value not in allowed:
        options = ", ".join(repr(option) for option in sorted(allowed))
        raise ConfigError(f"{name} must be one of {options}")
    return value


def _check_slack_live(settings: Settings) -> None:
    if settings.slack_mode != "live":
        return
    if settings.slack_transport == "bot":
        if not (settings.slack_bot_token and settings.slack_channels):
            raise ConfigError(
                "SLACK_MODE=live with SLACK_TRANSPORT=bot requires"
                " SLACK_BOT_TOKEN and SLACK_CHANNELS"
            )
    elif not settings.slack_destinations:
        raise ConfigError("SLACK_MODE=live requires SLACK_DESTINATIONS")


def load_settings() -> Settings:
    """Build settings from the environment, failing loudly on bad input."""
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        raise ConfigError("GITHUB_WEBHOOK_SECRET is required")

    devin_mode = _choice("DEVIN_MODE", "sim", frozenset({"sim", "live"}))
    slack_mode = _choice("SLACK_MODE", "fake", frozenset({"fake", "live"}))
    slack_transport = _choice(
        "SLACK_TRANSPORT", "webhook", frozenset({"webhook", "bot"})
    )
    review_gate_mode = _choice(
        "REVIEW_GATE_MODE",
        DEFAULT_REVIEW_GATE_MODE,
        frozenset({"off", "advisory", "required"}),
    )

    settings = Settings(
        github_webhook_secret=secret,
        repo_allowlist=frozenset(
            name.lower() for name in _split(os.environ.get("REPO_ALLOWLIST", ""))
        ),
        maintainer_allowlist=frozenset(
            login.lower()
            for login in _split(os.environ.get("MAINTAINER_ALLOWLIST", ""))
        ),
        approval_label=os.environ.get("APPROVAL_LABEL", DEFAULT_APPROVAL_LABEL),
        database_path=os.environ.get("DATABASE_PATH", "data/pipeline.db"),
        env="live" if devin_mode == "live" else "sim",
        devin_mode=devin_mode,
        devin_api_base=os.environ.get("DEVIN_API_BASE", "https://api.devin.ai"),
        devin_org_id=os.environ.get("DEVIN_ORG_ID", ""),
        devin_api_token=os.environ.get("DEVIN_API_TOKEN", ""),
        devin_playbook_id=os.environ.get("DEVIN_PLAYBOOK_ID", ""),
        devin_secret_ids=tuple(_split(os.environ.get("DEVIN_SECRET_IDS", ""))),
        slack_mode=slack_mode,
        slack_transport=slack_transport,
        slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
        slack_destinations=_parse_map(os.environ.get("SLACK_DESTINATIONS", "")),
        slack_channels=_parse_map(os.environ.get("SLACK_CHANNELS", "")),
        repo_destinations=_parse_map(os.environ.get("REPO_DESTINATIONS", "")),
        default_destination=os.environ.get(
            "DEFAULT_DESTINATION", "engineering-updates"
        ),
        operator_destination=os.environ.get(
            "OPERATOR_DESTINATION", "automation-alerts"
        ),
        max_acu_limit=_env_int("MAX_ACU_LIMIT", DEFAULT_MAX_ACU),
        max_concurrent_runs=_env_int(
            "MAX_CONCURRENT_RUNS", DEFAULT_MAX_CONCURRENT_RUNS
        ),
        max_daily_sessions=_env_int("MAX_DAILY_SESSIONS", DEFAULT_MAX_DAILY_SESSIONS),
        no_output_grace_seconds=_env_int(
            "NO_OUTPUT_GRACE_SECONDS", DEFAULT_NO_OUTPUT_GRACE_SECONDS
        ),
        run_max_seconds=_env_int("RUN_MAX_SECONDS", DEFAULT_RUN_MAX_SECONDS),
        lease_seconds=_env_int("LEASE_SECONDS", DEFAULT_LEASE_SECONDS),
        delivery_retention_days=_env_int("DELIVERY_RETENTION_DAYS", 30),
        dashboard_token=os.environ.get("DASHBOARD_TOKEN", ""),
        analytics_enabled=os.environ.get("ANALYTICS_ENABLED", "true").lower()
        not in {"0", "false", "no"},
        insights_refresh_seconds=_env_int(
            "INSIGHTS_REFRESH_SECONDS", DEFAULT_INSIGHTS_REFRESH_SECONDS
        ),
        insights_settle_seconds=_env_int(
            "INSIGHTS_SETTLE_SECONDS", DEFAULT_INSIGHTS_SETTLE_SECONDS
        ),
        metrics_refresh_seconds=_env_int(
            "METRICS_REFRESH_SECONDS", DEFAULT_METRICS_REFRESH_SECONDS
        ),
        review_gate_mode=review_gate_mode,
        review_poll_seconds=_env_int(
            "REVIEW_POLL_SECONDS", DEFAULT_REVIEW_POLL_SECONDS
        ),
        review_max_attempts=_env_int(
            "REVIEW_MAX_ATTEMPTS", DEFAULT_REVIEW_MAX_ATTEMPTS
        ),
    )

    if devin_mode == "live" and not (
        settings.devin_org_id and settings.devin_api_token
    ):
        raise ConfigError("DEVIN_MODE=live requires DEVIN_ORG_ID and DEVIN_API_TOKEN")
    if devin_mode == "live" and not settings.dashboard_token:
        raise ConfigError("DEVIN_MODE=live requires DASHBOARD_TOKEN")
    _check_slack_live(settings)
    return settings
