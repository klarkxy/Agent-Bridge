"""Kimi Code plan quota.

Kimi Code CLI's ``/usage`` is an interactive-shell command, and the
non-interactive ``kimi usage`` subcommand (kimi-cli PR #2301) has not landed.
The shell command itself is a plain ``GET <Kimi Code base_url>/usages`` with
the OAuth access token ``kimi login`` stores under
``<KIMI_CODE_HOME>/credentials/kimi-code.json``; Bridge makes the same call.

Only the Kimi Code platform has this endpoint. API-key sessions bill the
Moonshot Open Platform and answer ``unknown``. Bridge reads the credential
file and never refreshes or rewrites it: an expired token is a reason string,
not something to fix from here.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_bridge.config import AgentConfig
from agent_bridge.kimi_observe import kimi_home
from agent_bridge.quota import (
    QuotaStatus,
    QuotaWindow,
    as_float,
    get_json,
    summarize_quota,
    unknown_quota,
    window_from_reset,
    window_name_from_minutes,
)
from agent_bridge.quota_endpoints import official_url, quota_block_reason

SOURCE = "kimi code GET /usages"
DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
CREDENTIAL_FILE = "kimi-code.json"


def kimi_usage_url(env: Mapping[str, str]) -> str:
    base = (env.get("KIMI_CODE_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    if not official_url(base, DEFAULT_BASE_URL):
        raise ValueError("quota lookup is not supported for custom endpoints")
    return f"{DEFAULT_BASE_URL}/usages"


def kimi_access_token(home: Path, *, now: float | None = None) -> tuple[str | None, str | None]:
    """Return ``(token, problem)``; exactly one is set."""
    creds = home / "credentials"
    path = creds / CREDENTIAL_FILE
    if not path.is_file():
        return None, "Kimi Code quota needs `kimi login` (OAuth); no credential file found"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"could not read {path.name}: {type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, f"{path.name} is not a credential object"
    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None, f"{path.name} has no access_token; run `kimi login`"
    expires_at = as_float(payload.get("expires_at"))
    if expires_at and expires_at <= (now if now is not None else time.time()):
        return None, "Kimi Code access token has expired; open `kimi` once so it refreshes"
    return token.strip(), None


def _window_label(item: Mapping[str, Any], detail: Mapping[str, Any], window: Mapping[str, Any], index: int) -> str:
    for key in ("name", "title", "scope"):
        value = item.get(key) or detail.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    duration = as_float(window.get("duration") or item.get("duration") or detail.get("duration"))
    unit = str(window.get("timeUnit") or item.get("timeUnit") or detail.get("timeUnit") or "").upper()
    if duration:
        if "MINUTE" in unit:
            return window_name_from_minutes(duration, f"{int(duration)}m")
        if "HOUR" in unit:
            return window_name_from_minutes(duration * 60, f"{int(duration)}h")
        if "DAY" in unit:
            return window_name_from_minutes(duration * 1440, f"{int(duration)}d")
        if "SECOND" in unit:
            return window_name_from_minutes(duration / 60, f"{int(duration)}s")
    return f"limit-{index + 1}"


def _remaining_percent(data: Mapping[str, Any]) -> float | None:
    limit = as_float(data.get("limit"))
    if limit is None or limit <= 0:
        return None
    used = as_float(data.get("used"))
    remaining = as_float(data.get("remaining"))
    if remaining is None and used is not None:
        remaining = limit - used
    if remaining is None:
        return None
    return round(min(max(remaining / limit * 100.0, 0.0), 100.0), 2)


def _reset_value(data: Mapping[str, Any]) -> Any:
    for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
        if data.get(key):
            return data[key]
    for key in ("reset_in", "resetIn", "ttl"):
        seconds = as_float(data.get(key))
        if seconds:
            return time.time() + seconds
    return None


def parse_kimi_usage(payload: Mapping[str, Any] | None) -> QuotaStatus:
    if not isinstance(payload, Mapping):
        return unknown_quota("kimi returned no usage payload", source=SOURCE)
    windows: list[QuotaWindow] = []
    summary = payload.get("usage")
    if isinstance(summary, Mapping):
        label = summary.get("name") or summary.get("title") or "weekly"
        windows.append(window_from_reset(str(label), _remaining_percent(summary), _reset_value(summary)))
    limits = payload.get("limits")
    if isinstance(limits, Sequence) and not isinstance(limits, str | bytes):
        for index, item in enumerate(limits):
            if not isinstance(item, Mapping):
                continue
            detail_raw = item.get("detail")
            detail: Mapping[str, Any] = detail_raw if isinstance(detail_raw, Mapping) else item
            window_raw = item.get("window")
            window: Mapping[str, Any] = window_raw if isinstance(window_raw, Mapping) else {}
            remaining = _remaining_percent(detail)
            if remaining is None and as_float(detail.get("limit")) is None:
                continue
            windows.append(
                window_from_reset(_window_label(item, detail, window, index), remaining, _reset_value(detail))
            )
    return summarize_quota(windows, source=SOURCE, detail=None if windows else "kimi usage payload had no limits")


async def fetch_kimi_quota(cfg: AgentConfig, env: Mapping[str, str]) -> QuotaStatus:
    if reason := quota_block_reason(cfg, env):
        return unknown_quota(reason, source=SOURCE)
    raw_home = (env.get("KIMI_CODE_HOME") or "").strip()
    home = kimi_home(Path(raw_home) if raw_home else None)
    token, problem = kimi_access_token(home)
    if token is None:
        if env.get("KIMI_API_KEY") or env.get("MOONSHOT_API_KEY"):
            problem = (
                "Kimi API-key sessions bill the Moonshot Open Platform, which has no quota endpoint; "
                "quota is only available after `kimi login`"
            )
        return unknown_quota(problem or "no Kimi Code credential", source=SOURCE)
    payload = await get_json(
        kimi_usage_url(env),
        headers={"Authorization": f"Bearer {token}"},
        env=env,
        timeout=10.0,
    )
    return parse_kimi_usage(payload)
