"""Codex CLI plan usage through ``codex app-server``.

The interactive ``/status`` panel has no headless twin, but Codex ships an
``app-server`` subcommand speaking JSON-RPC over stdio, and its
``account/rateLimits/read`` answers with the same primary (5h) and secondary
(weekly) windows the panel shows. OpenAI documents the server but marks it
experimental, so parsing here tolerates missing fields and turns any surprise
into ``unknown`` rather than a crash.

One short-lived process per lookup; the registry cache keeps that rare.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

from agent_bridge.codex_exec import resolve_codex_command
from agent_bridge.config import AgentConfig
from agent_bridge.processes import reap_subprocess
from agent_bridge.quota import (
    QuotaBalance,
    QuotaStatus,
    QuotaWindow,
    as_float,
    clamp_percent,
    summarize_quota,
    unknown_quota,
    window_from_reset,
    window_name_from_minutes,
)

SOURCE = "codex app-server account/rateLimits/read"
CLIENT_INFO = {"name": "agent-bridge", "title": "Agent Bridge", "version": "1"}
# Codex answers `initialize` before its account client is ready; an immediate
# rateLimits read returns empty. The community tools all settle on ~500ms.
POST_INIT_SETTLE_SEC = 0.5
READ_LIMIT = 1024 * 1024


def parse_codex_rate_limits(result: Mapping[str, Any] | None) -> QuotaStatus:
    """Turn an ``account/rateLimits/read`` result into a QuotaStatus."""
    if not isinstance(result, Mapping):
        return unknown_quota("codex returned no rateLimits payload", source=SOURCE)
    limits = result.get("rateLimits")
    if not isinstance(limits, Mapping):
        return unknown_quota(
            "codex returned no rateLimits (API-key logins have no plan quota; sign in with ChatGPT)",
            source=SOURCE,
        )
    windows: list[QuotaWindow] = []
    for key, default_name in (("primary", "5h"), ("secondary", "weekly")):
        window = limits.get(key)
        if not isinstance(window, Mapping):
            continue
        used = clamp_percent(window.get("usedPercent"))
        remaining = None if used is None else round(100.0 - used, 2)
        name = window_name_from_minutes(window.get("windowDurationMins"), default_name)
        windows.append(window_from_reset(name, remaining, window.get("resetsAt")))

    balance: QuotaBalance | None = None
    credits = limits.get("credits")
    if isinstance(credits, Mapping) and credits.get("hasCredits"):
        raw = credits.get("balance")
        if credits.get("unlimited"):
            balance = QuotaBalance(amount="unlimited")
        elif raw is not None and as_float(raw) is not None:
            balance = QuotaBalance(amount=str(raw), currency="USD")

    plan = limits.get("planType")
    reached = limits.get("rateLimitReachedType")
    detail = None
    if isinstance(reached, str) and reached:
        detail = f"codex reports rate limit reached: {reached}"
    return summarize_quota(
        windows,
        balance=balance,
        plan=str(plan) if isinstance(plan, str) and plan else None,
        source=SOURCE,
        detail=detail,
        exhausted=bool(reached),
    )


def _error_text(error: Any) -> str:
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
        return json.dumps(error, ensure_ascii=False)[:200]
    return str(error)[:200]


async def _read_response(proc: asyncio.subprocess.Process, wanted_id: int) -> Mapping[str, Any]:
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            raise RuntimeError("codex app-server closed stdout before answering")
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, Mapping) or message.get("id") != wanted_id:
            continue
        if "error" in message:
            error = _error_text(message["error"])
            lowered = error.lower()
            if any(mark in lowered for mark in ("token_invalidated", "401", "unauthorized", "authentication required")):
                raise RuntimeError(f"codex is not signed in ({error}); run `codex login`")
            raise RuntimeError(f"codex app-server error: {error}")
        result = message.get("result")
        return result if isinstance(result, Mapping) else {}


async def _send(proc: asyncio.subprocess.Process, payload: Mapping[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def read_codex_rate_limits(command: list[str], env: Mapping[str, str]) -> Mapping[str, Any]:
    """One-shot JSON-RPC exchange: initialize, initialized, account/rateLimits/read."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = await asyncio.create_subprocess_exec(
        *command,
        "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=dict(env),
        limit=READ_LIMIT,
        **kwargs,
    )
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": CLIENT_INFO}})
        await _read_response(proc, 1)
        await _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
        await asyncio.sleep(POST_INIT_SETTLE_SEC)
        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
        return await _read_response(proc, 2)
    finally:
        await reap_subprocess(proc, timeout=2.0)


async def fetch_codex_quota(cfg: AgentConfig, env: Mapping[str, str]) -> QuotaStatus:
    command = await asyncio.to_thread(resolve_codex_command, cfg.command, cfg.fallback_commands, env=env)
    result = await read_codex_rate_limits(command, env)
    return parse_codex_rate_limits(result)


fetch_codex_quota.quota_source = SOURCE  # type: ignore[attr-defined]
