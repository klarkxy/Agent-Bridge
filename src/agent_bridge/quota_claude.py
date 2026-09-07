"""Claude Code plan usage (experimental).

Claude Code's ``/usage`` panel calls ``GET https://api.anthropic.com/api/oauth/usage``
with the claude.ai OAuth token from ``~/.claude/.credentials.json``. It is not
a documented API and it rate-limits aggressively unless the request carries a
``claude-code/<version>`` User-Agent, so it sits behind ``[quota] experimental``
and relies on the registry cache (five minutes by default). A 429 is raised so
``fetch_quota`` falls back to the last good reading instead of caching a miss.

Only an OAuth login has a plan. API-key and gateway sessions answer ``unknown``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_bridge.claude_meta import apply_claude_gateway_env, claude_config_home, describe_claude_auth
from agent_bridge.config import AgentConfig
from agent_bridge.quota import (
    QuotaStatus,
    QuotaWindow,
    as_float,
    clamp_percent,
    get_json,
    summarize_quota,
    unknown_quota,
    window_from_reset,
)

SOURCE = "claude GET /api/oauth/usage (experimental)"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
# The endpoint buckets rate limits by User-Agent; anything else gets 429s.
USER_AGENT = "claude-code/2.1.80"
WINDOWS = (
    ("five_hour", "5h"),
    ("seven_day", "weekly"),
    ("seven_day_opus", "weekly:opus"),
    ("seven_day_sonnet", "weekly:sonnet"),
)


def claude_access_token(home: Path, *, now: float | None = None) -> tuple[str | None, str | None]:
    for name in (".credentials.json", "credentials.json"):
        path = home / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return None, f"could not read {name}: {type(exc).__name__}"
        oauth = payload.get("claudeAiOauth") if isinstance(payload, Mapping) else None
        if not isinstance(oauth, Mapping):
            return None, f"{name} has no claudeAiOauth block; run `claude auth login`"
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token.strip():
            return None, f"{name} has no accessToken; run `claude auth login`"
        expires = as_float(oauth.get("expiresAt"))
        if expires:
            if expires > 1e11:
                expires /= 1000.0
            if expires <= (now if now is not None else time.time()):
                return None, "Claude Code OAuth token has expired; open `claude` once so it refreshes"
        return token.strip(), None
    return None, "Claude Code plan usage needs `claude auth login`; no credentials file found"


def parse_claude_usage(payload: Mapping[str, Any] | None) -> QuotaStatus:
    if not isinstance(payload, Mapping):
        return unknown_quota("claude returned no usage payload", source=SOURCE)
    windows: list[QuotaWindow] = []
    for key, name in WINDOWS:
        block = payload.get(key)
        if not isinstance(block, Mapping):
            continue
        utilization = clamp_percent(block.get("utilization", block.get("used_percentage")))
        remaining = None if utilization is None else round(100.0 - utilization, 2)
        windows.append(window_from_reset(name, remaining, block.get("resets_at")))
    if not windows:
        return unknown_quota("claude usage payload had no five_hour/seven_day windows", source=SOURCE)
    return summarize_quota(windows, source=SOURCE)


async def fetch_claude_quota(cfg: AgentConfig, env: Mapping[str, str]) -> QuotaStatus:
    auth = describe_claude_auth(env)
    if not auth.startswith("oauth"):
        return unknown_quota(
            f"Claude plan usage is only available for a claude.ai login (auth={auth})",
            source=SOURCE,
        )
    resolved = apply_claude_gateway_env(env)
    token, problem = claude_access_token(claude_config_home(resolved))
    if token is None:
        return unknown_quota(problem or "no Claude Code credential", source=SOURCE)
    payload = await get_json(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": USER_AGENT,
        },
        env=env,
        timeout=10.0,
    )
    return parse_claude_usage(payload)


fetch_claude_quota.quota_source = SOURCE  # type: ignore[attr-defined]
