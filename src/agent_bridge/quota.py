"""Remaining-quota lookup for worker CLIs, returned inline by ``list_agents``.

Bridge does not route on quota. It relays "how much plan is left and when it
resets" so the coordinator can weigh that against ``coordinator.instructions``
and the user's own preferences. A worker that has run dry otherwise looks
like any other failed turn, and the coordinator quietly picks the work up
itself — spending a budget the user never meant to spend.

Every lookup is bounded: one timeout per worker, one cache entry per worker,
and every failure mode collapses to ``status: "unknown"`` with the reason in
``detail``. ``unknown`` means "could not tell", never "no quota left", and it
never changes ``available``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from agent_bridge.config import AgentConfig, AppConfig
from agent_bridge.models import iso, utcnow

log = logging.getLogger(__name__)

QUOTA_STATUSES = ("ok", "exhausted", "unknown")

# Failures are cached for a short spell so a hung CLI cannot add timeout_sec
# to every list_agents call, while a transient blip clears quickly.
FAILURE_CACHE_SEC = 60.0

# Task errors carrying one of these phrases drop the worker's cache entry so
# the next list_agents re-reads the live number instead of a stale "ok".
QUOTA_ERROR_MARKERS = (
    "quota",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "usage limit",
    "insufficient balance",
    "insufficient_quota",
    "billing",
    "credits",
    "exceeded",
)


class QuotaWindow(BaseModel):
    """One rolling limit: ``5h``, ``weekly``, ``daily`` or whatever the CLI names it."""

    name: str
    remaining_percent: float | None = None
    resets_at: str | None = None
    resets_in_sec: int | None = None


class QuotaBalance(BaseModel):
    amount: str
    currency: str | None = None


class QuotaStatus(BaseModel):
    status: str = "unknown"
    windows: list[QuotaWindow] = Field(default_factory=list)
    balance: QuotaBalance | None = None
    plan: str | None = None
    source: str | None = None
    fetched_at: str | None = None
    cached: bool = False
    stale: bool = False
    detail: str | None = None


QuotaProvider = Callable[[AgentConfig, Mapping[str, str]], Awaitable[QuotaStatus]]


def unknown_quota(detail: str, *, source: str | None = None) -> QuotaStatus:
    return QuotaStatus(status="unknown", detail=detail, source=source, fetched_at=iso())


def summarize_quota(
    windows: list[QuotaWindow],
    *,
    balance: QuotaBalance | None = None,
    plan: str | None = None,
    source: str | None = None,
    detail: str | None = None,
    exhausted: bool = False,
) -> QuotaStatus:
    """Build an ``ok`` / ``exhausted`` status from parsed windows and balance.

    ``exhausted`` is set when any window is fully used or the caller says the
    account is blocked (DeepSeek ``is_available: false``, Codex
    ``rateLimitReachedType``). Nothing parsed at all is ``unknown``: an empty
    answer is not evidence of a full quota.
    """
    if not windows and balance is None:
        return unknown_quota(detail or "no quota data in the response", source=source)
    dry = exhausted or any(
        window.remaining_percent is not None and window.remaining_percent <= 0 for window in windows
    )
    return QuotaStatus(
        status="exhausted" if dry else "ok",
        windows=windows,
        balance=balance,
        plan=plan,
        source=source,
        detail=detail,
        fetched_at=iso(),
    )


def clamp_percent(value: Any) -> float | None:
    number = as_float(value)
    if number is None:
        return None
    return round(min(max(number, 0.0), 100.0), 2)


def as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_timestamp(value: Any) -> datetime | None:
    """Accept epoch seconds, epoch milliseconds, or an ISO-8601 string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        if number <= 0:
            return None
        # Anything past year 5000 in seconds is really milliseconds.
        if number > 1e11:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.replace(".", "", 1).isdigit():
            return parse_timestamp(float(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # Truncate nanosecond fractions Python cannot parse.
        if "." in text:
            head, _, tail = text.partition(".")
            frac = ""
            rest = tail
            while rest and rest[0].isdigit():
                frac += rest[0]
                rest = rest[1:]
            text = f"{head}.{frac[:6]}{rest}" if frac else f"{head}{rest}"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def window_from_reset(
    name: str,
    remaining_percent: float | None,
    resets: Any,
    *,
    now: datetime | None = None,
) -> QuotaWindow:
    moment = parse_timestamp(resets)
    resets_at: str | None = None
    resets_in: int | None = None
    if moment is not None:
        resets_at = moment.isoformat()
        resets_in = max(int((moment - (now or utcnow())).total_seconds()), 0)
    return QuotaWindow(
        name=name,
        remaining_percent=remaining_percent,
        resets_at=resets_at,
        resets_in_sec=resets_in,
    )


def window_name_from_minutes(minutes: Any, default: str) -> str:
    total = as_float(minutes)
    if total is None or total <= 0:
        return default
    total = int(total)
    if total == 300:
        return "5h"
    if total == 1440:
        return "daily"
    if total == 10080:
        return "weekly"
    if total % 1440 == 0:
        return f"{total // 1440}d"
    if total % 60 == 0:
        return f"{total // 60}h"
    return f"{total}m"


class QuotaHTTPError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if message else f"HTTP {status}")
        self.status = status


def _proxy_map(env: Mapping[str, str]) -> dict[str, str]:
    proxies: dict[str, str] = {}
    for scheme in ("https", "http"):
        for key in (f"{scheme.upper()}_PROXY", f"{scheme}_proxy"):
            value = (env.get(key) or "").strip()
            if value:
                proxies[scheme] = value
                break
    return proxies


def _get_json_sync(url: str, headers: Mapping[str, str], env: Mapping[str, str], timeout: float) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(_proxy_map(env)))
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):
            body = ""
        message = body.strip().replace("\n", " ")[:160]
        raise QuotaHTTPError(exc.code, message) from None
    return json.loads(raw.decode("utf-8"))


async def get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> Any:
    """GET a JSON document through the worker's proxy settings, off the loop.

    Standard library only: quota lookups must not add a runtime dependency.
    Auth headers are passed in by the caller and never logged here.
    """
    return await asyncio.to_thread(_get_json_sync, url, headers or {}, env or {}, timeout)


class QuotaCache:
    """Per-worker memo with an expiry, plus the last good answer for fallback."""

    def __init__(self, ttl_sec: float) -> None:
        self.ttl_sec = max(float(ttl_sec), 0.0)
        self._fresh: dict[str, tuple[float, QuotaStatus]] = {}
        self._last_good: dict[str, QuotaStatus] = {}

    def get(self, name: str, *, now: float | None = None) -> QuotaStatus | None:
        entry = self._fresh.get(name)
        if entry is None:
            return None
        expires_at, status = entry
        if (now if now is not None else time.monotonic()) >= expires_at:
            self._fresh.pop(name, None)
            return None
        return status

    def last_good(self, name: str) -> QuotaStatus | None:
        return self._last_good.get(name)

    def put(self, name: str, status: QuotaStatus, *, ttl_sec: float | None = None) -> None:
        ttl = self.ttl_sec if ttl_sec is None else max(float(ttl_sec), 0.0)
        if ttl > 0:
            self._fresh[name] = (time.monotonic() + ttl, status)
        if status.status in ("ok", "exhausted"):
            self._last_good[name] = status

    def invalidate(self, name: str) -> None:
        self._fresh.pop(name, None)

    def clear(self) -> None:
        self._fresh.clear()
        self._last_good.clear()


def _dump(status: QuotaStatus) -> dict[str, Any]:
    return status.model_dump(mode="json")


def _stale_or_unknown(cache: QuotaCache, name: str, reason: str, source: str | None) -> QuotaStatus:
    last = cache.last_good(name)
    if last is None:
        return unknown_quota(reason, source=source)
    return last.model_copy(
        update={
            "cached": True,
            "stale": True,
            "detail": f"{reason}; showing the last successful reading from {last.fetched_at}",
        }
    )


def looks_like_quota_error(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in QUOTA_ERROR_MARKERS)


def resolve_provider(
    cfg: AgentConfig,
    providers: Mapping[str, QuotaProvider],
) -> QuotaProvider | None:
    """Agent name first, then ``protocol:<name>`` — the same split ``probe_agent`` uses.

    Codex is looked up by protocol so a renamed ``[agents.codex-alt]`` block
    still reaches the app-server provider; ACP workers are looked up by name
    because ``protocol = "acp"`` says nothing about which product it is.
    """
    by_name = providers.get(cfg.name)
    if by_name is not None:
        return by_name
    return providers.get(f"protocol:{cfg.protocol}")


async def fetch_quota(
    cfg: AgentConfig,
    env: Mapping[str, str],
    *,
    cache: QuotaCache,
    timeout_sec: float,
    providers: Mapping[str, QuotaProvider] | None = None,
) -> dict[str, Any]:
    """Return the ``quota`` dict for one worker. Never raises."""
    hit = cache.get(cfg.name)
    if hit is not None:
        return _dump(hit.model_copy(update={"cached": True}))

    table = default_providers() if providers is None else providers
    provider = resolve_provider(cfg, table)
    if provider is None:
        status = unknown_quota(f"quota lookup is not supported for {cfg.name}")
        cache.put(cfg.name, status)
        return _dump(status)

    source = getattr(provider, "quota_source", None)
    try:
        status = await asyncio.wait_for(provider(cfg, env), timeout=timeout_sec)
    except TimeoutError:
        log.info("quota lookup for %s timed out after %.1fs", cfg.name, timeout_sec)
        status = _stale_or_unknown(cache, cfg.name, f"quota lookup timed out after {timeout_sec:g}s", source)
        cache.put(cfg.name, status, ttl_sec=min(cache.ttl_sec, FAILURE_CACHE_SEC))
        return _dump(status)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.info("quota lookup for %s failed: %s: %s", cfg.name, type(exc).__name__, exc)
        status = _stale_or_unknown(cache, cfg.name, f"quota lookup failed: {type(exc).__name__}: {exc}", source)
        cache.put(cfg.name, status, ttl_sec=min(cache.ttl_sec, FAILURE_CACHE_SEC))
        return _dump(status)

    if status.status not in QUOTA_STATUSES:
        status = status.model_copy(update={"status": "unknown"})
    if status.fetched_at is None:
        status = status.model_copy(update={"fetched_at": iso()})
    if status.source is None and source:
        status = status.model_copy(update={"source": source})
    ttl = cache.ttl_sec if status.status != "unknown" else min(cache.ttl_sec, FAILURE_CACHE_SEC)
    cache.put(cfg.name, status, ttl_sec=ttl)
    return _dump(status)


def default_providers(*, experimental: bool = False) -> dict[str, QuotaProvider]:
    """Provider table. Imported lazily so ``quota`` stays import-light for tests.

    Codex, Kimi Code and DeepSeek use documented or CLI-official paths. Grok
    Build and Claude Code only expose their plan usage through the endpoints
    their own ``/usage`` commands call; those are behind ``[quota] experimental``
    because they can change without notice.
    """
    from agent_bridge.quota_codex import fetch_codex_quota
    from agent_bridge.quota_deepseek import fetch_dsh_quota
    from agent_bridge.quota_kimi import fetch_kimi_quota

    table: dict[str, QuotaProvider] = {
        "protocol:codex": fetch_codex_quota,
        "kimi": fetch_kimi_quota,
        "dsh": fetch_dsh_quota,
    }
    if experimental:
        from agent_bridge.quota_claude import fetch_claude_quota
        from agent_bridge.quota_grok import fetch_grok_quota

        table["grok"] = fetch_grok_quota
        table["claude"] = fetch_claude_quota
    return table


EXPERIMENTAL_HINT = (
    "reads the endpoint the CLI's own /usage command calls; enable with [quota] experimental = true"
)


def experimental_placeholders() -> dict[str, QuotaProvider]:
    """Explain, rather than silently skip, the workers gated behind the flag."""

    async def grok_placeholder(cfg: AgentConfig, env: Mapping[str, str]) -> QuotaStatus:
        return unknown_quota(f"Grok Build quota {EXPERIMENTAL_HINT}")

    async def claude_placeholder(cfg: AgentConfig, env: Mapping[str, str]) -> QuotaStatus:
        return unknown_quota(f"Claude Code plan usage {EXPERIMENTAL_HINT}")

    return {"grok": grok_placeholder, "claude": claude_placeholder}


def provider_table(config: AppConfig) -> dict[str, QuotaProvider]:
    table = default_providers(experimental=config.quota.experimental)
    if not config.quota.experimental:
        for name, placeholder in experimental_placeholders().items():
            table.setdefault(name, placeholder)
    return table


async def describe_quotas(config: AppConfig, *, bypass_cache: bool = True) -> dict[str, Any]:
    """Fresh quota for every configured worker — the ``agent-bridge --quota`` payload."""
    from agent_bridge.probes import command_exists
    from agent_bridge.worker_env import build_worker_env

    cache = QuotaCache(0.0 if bypass_cache else config.quota.cache_sec)
    table = provider_table(config)

    async def one(cfg: AgentConfig) -> tuple[str, dict[str, Any]]:
        if not await asyncio.to_thread(command_exists, cfg):
            return cfg.name, _dump(unknown_quota("worker command not found"))
        env = await asyncio.to_thread(build_worker_env, cfg.env, config=config.env, log_fill=False)
        return cfg.name, await fetch_quota(
            cfg,
            env,
            cache=cache,
            timeout_sec=config.quota.timeout_sec,
            providers=table,
        )

    rows = await asyncio.gather(*(one(cfg) for cfg in config.agents.values()))
    return dict(rows)
