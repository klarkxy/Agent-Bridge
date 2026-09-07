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

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_bridge.config import AgentConfig
from agent_bridge.grok_observe import grok_home
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

SOURCE = "grok build GET /v1/billing?format=credits (experimental)"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
OIDC_MARKER = "auth.x.ai"


def grok_auth_token(home: Path, *, now: float | None = None) -> tuple[str | None, str | None]:
    """Return ``(token, problem)`` from ``auth.json``: ``{"<scope>": {"key": ..., "expires_at": ...}}``."""
    path = home / "auth.json"
    if not path.is_file():
        return None, "Grok Build quota needs `grok login`; no auth.json found"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"could not read auth.json: {type(exc).__name__}"
    if not isinstance(payload, Mapping):
        return None, "auth.json is not an object"
    entries = [(str(scope), entry) for scope, entry in payload.items() if isinstance(entry, Mapping)]
    entries.sort(key=lambda pair: 0 if OIDC_MARKER in pair[0] else 1)
    for _scope, entry in entries:
        key = entry.get("key") or entry.get("access_token")
        if not isinstance(key, str) or not key.strip():
            continue
        expires = as_float(entry.get("expires_at"))
        if expires:
            if expires > 1e11:
                expires /= 1000.0
            if expires <= (now if now is not None else time.time()):
                return None, "Grok Build session token has expired; open `grok` once so it refreshes"
        return key.strip(), None
    return None, "auth.json holds no session key; run `grok login`"


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
    raw_home = (env.get("GROK_HOME") or "").strip()
    token, problem = grok_auth_token(grok_home(Path(raw_home) if raw_home else None))
    if token is None:
        if env.get("XAI_API_KEY") or env.get("GROK_API_KEY"):
            problem = "Grok Build API-key sessions have no subscription quota; sign in with `grok login` to see one"
        return unknown_quota(problem or "no Grok Build session", source=SOURCE)
    payload = await get_json(
        BILLING_URL,
        headers={"Authorization": f"Bearer {token}"},
        env=env,
        timeout=10.0,
    )
    return parse_grok_billing(payload)


fetch_grok_quota.quota_source = SOURCE  # type: ignore[attr-defined]
