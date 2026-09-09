"""Grok Build weekly credit usage (experimental).

Grok Build's ``/usage`` (alias ``/cost``) reads
``GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`` with the
session token from ``~/.grok/auth.json``. There is no documented CLI command
for it, so this lives behind ``[quota] experimental``. Bridge reads the auth
file and never refreshes the token — Grok Build owns that file.

API-key sessions (``XAI_API_KEY``) have no subscription quota; the CLI hides
``/usage`` for them and so does Bridge.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_bridge.config import AgentConfig
from agent_bridge.grok_observe import grok_home
from agent_bridge.processes import reap_subprocess, resolve_command
from agent_bridge.quota import (
    QuotaStatus,
    QuotaWindow,
    clamp_percent,
    get_json,
    parse_timestamp,
    summarize_quota,
    unknown_quota,
    window_from_reset,
)
from agent_bridge.quota_endpoints import GROK_AUTH, GROK_AUTH_SCOPE, quota_block_reason

SOURCE = "grok build GET /v1/billing?format=credits (experimental)"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
def grok_auth_headers(
    home: Path, *, version: str, auth_path: Path | None = None, now: float | None = None,
) -> tuple[dict[str, str] | None, str | None]:
    """Read only the supported default CLI scope and validate its issuer."""
    path = auth_path or home / "auth.json"
    if not path.is_file():
        return None, "Grok Build quota needs `grok login`; no auth.json found"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"could not read auth.json: {type(exc).__name__}"
    if not isinstance(payload, Mapping):
        return None, "auth.json is not an object"
    entry = payload.get(GROK_AUTH_SCOPE)
    if not isinstance(entry, Mapping):
        return None, "auth.json has no default Grok OAuth scope; run `grok login`"
    if entry.get("auth_mode") not in ("oidc", "external") or entry.get("oidc_issuer") != GROK_AUTH:
        return None, "Grok quota requires a verified first-party xAI OAuth entry"
    key, user_id = entry.get("key"), entry.get("user_id")
    if not isinstance(key, str) or not key.strip() or not isinstance(user_id, str) or not user_id.strip():
        return None, "Grok OAuth entry is missing its session key or user_id"
    expires = parse_timestamp(entry.get("expires_at"))
    if expires is None:
        return None, "Grok OAuth expiry cannot be verified; open `grok` to refresh the login"
    if expires <= (datetime.now(UTC) if now is None else datetime.fromtimestamp(now, UTC)):
        return None, "Grok Build session token has expired; open `grok` once so it refreshes"
    return {
        "Authorization": f"Bearer {key.strip()}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-userid": user_id,
        "x-grok-client-version": version,
        "x-grok-client-mode": "headless",
    }, None


async def _grok_version(cfg: AgentConfig, env: Mapping[str, str]) -> str | None:
    command = await asyncio.to_thread(resolve_command, cfg.command, cfg.fallback_commands)
    proc = await asyncio.create_subprocess_exec(
        command[0], "--no-auto-update", "--version", env=dict(env),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await proc.communicate()
        match = re.search(r"\bgrok\s+(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", out.decode("utf-8", errors="replace"))
        return match.group(1) if match else None
    finally:
        await reap_subprocess(proc)


def _period_name(period: Mapping[str, Any]) -> str:
    kind = str(period.get("type") or "").upper()
    if "WEEK" in kind:
        return "weekly"
    if "DAY" in kind or "DAILY" in kind:
        return "daily"
    if "MONTH" in kind:
        return "monthly"
    return "period"


def parse_grok_billing(payload: Mapping[str, Any] | None) -> QuotaStatus:
    if not isinstance(payload, Mapping):
        return unknown_quota("grok returned no billing payload", source=SOURCE)
    config = payload.get("config")
    if not isinstance(config, Mapping):
        config = payload
    used = clamp_percent(config.get("creditUsagePercent", config.get("credit_usage_percent")))
    period_raw = config.get("currentPeriod", config.get("current_period"))
    period: Mapping[str, Any] = period_raw if isinstance(period_raw, Mapping) else {}
    windows: list[QuotaWindow] = []
    if used is not None:
        windows.append(window_from_reset(_period_name(period), round(100.0 - used, 2), period.get("end")))
    products = config.get("productUsage", config.get("product_usage"))
    notes: list[str] = []
    if isinstance(products, Sequence) and not isinstance(products, str | bytes):
        for item in products:
            if not isinstance(item, Mapping):
                continue
            name = item.get("product")
            pct = clamp_percent(item.get("usagePercent", item.get("usage_percent")))
            if isinstance(name, str) and pct is not None:
                notes.append(f"{name} {pct:g}% used")
    detail = "; ".join(notes) or None
    if not windows:
        return unknown_quota(detail or "grok billing payload had no creditUsagePercent", source=SOURCE)
    return summarize_quota(windows, source=SOURCE, detail=detail)


async def fetch_grok_quota(cfg: AgentConfig, env: Mapping[str, str]) -> QuotaStatus:
    if reason := quota_block_reason(cfg, env):
        return unknown_quota(reason, source=SOURCE)
    if env.get("XAI_API_KEY") or env.get("GROK_API_KEY") or env.get("GROK_CODE_XAI_API_KEY"):
        return unknown_quota("Grok Build API-key sessions have no subscription quota", source=SOURCE)
    raw_home = (env.get("GROK_HOME") or "").strip()
    version = await _grok_version(cfg, env)
    if version is None:
        return unknown_quota("could not identify the Grok CLI version for quota headers", source=SOURCE)
    headers, problem = grok_auth_headers(
        grok_home(Path(raw_home) if raw_home else None), version=version,
        auth_path=Path(env["GROK_AUTH_PATH"]) if env.get("GROK_AUTH_PATH") else None,
    )
    if headers is None:
        return unknown_quota(problem or "no Grok Build session", source=SOURCE)
    payload = await get_json(
        BILLING_URL,
        headers=headers,
        env=env,
        timeout=10.0,
    )
    return parse_grok_billing(payload)
