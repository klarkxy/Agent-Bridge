from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_bridge.config import AgentConfig, AppConfig, QuotaConfig, load_config
from agent_bridge.quota import (
    FAILURE_CACHE_SEC,
    QuotaBalance,
    QuotaCache,
    QuotaStatus,
    QuotaWindow,
    _proxy_map,
    default_providers,
    describe_quotas,
    fetch_quota,
    looks_like_quota_error,
    parse_timestamp,
    provider_table,
    resolve_provider,
    summarize_quota,
    unknown_quota,
    window_from_reset,
    window_name_from_minutes,
)
from agent_bridge.quota_claude import claude_access_token, fetch_claude_quota, parse_claude_usage
from agent_bridge.quota_codex import fetch_codex_quota, parse_codex_rate_limits
from agent_bridge.quota_deepseek import fetch_dsh_quota, parse_deepseek_balance
from agent_bridge.quota_grok import fetch_grok_quota, grok_auth_token, parse_grok_billing
from agent_bridge.quota_kimi import fetch_kimi_quota, kimi_access_token, kimi_usage_url, parse_kimi_usage

FAKE_CODEX = Path(__file__).with_name("fake_codex.py")
FAR_FUTURE = 4102444800  # 2100-01-01T00:00:00Z


def _agent(name: str = "fake", protocol: str = "fake") -> AgentConfig:
    return AgentConfig(name=name, protocol=protocol, command=[name])


# --- shared helpers -----------------------------------------------------------


def test_parse_timestamp_accepts_epoch_seconds_millis_and_iso():
    seconds = parse_timestamp(FAR_FUTURE)
    millis = parse_timestamp(FAR_FUTURE * 1000)
    iso_z = parse_timestamp("2100-01-01T00:00:00Z")
    iso_nanos = parse_timestamp("2100-01-01T00:00:00.123456789Z")
    numeric_text = parse_timestamp(str(FAR_FUTURE))
    assert seconds == millis == iso_z == numeric_text == datetime(2100, 1, 1, tzinfo=UTC)
    assert iso_nanos is not None and iso_nanos.microsecond == 123456
    assert parse_timestamp(None) is None
    assert parse_timestamp(True) is None
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(0) is None


def test_window_from_reset_reports_seconds_until_reset():
    now = datetime(2099, 12, 31, 23, 0, tzinfo=UTC)
    window = window_from_reset("5h", 88.0, FAR_FUTURE, now=now)
    assert window.resets_at == "2100-01-01T00:00:00+00:00"
    assert window.resets_in_sec == 3600
    past = window_from_reset("5h", 88.0, "2000-01-01T00:00:00Z", now=now)
    assert past.resets_in_sec == 0
    assert window_from_reset("5h", None, None).resets_at is None


def test_window_name_from_minutes():
    assert window_name_from_minutes(300, "x") == "5h"
    assert window_name_from_minutes(10080, "x") == "weekly"
    assert window_name_from_minutes(1440, "x") == "daily"
    assert window_name_from_minutes(2880, "x") == "2d"
    assert window_name_from_minutes(120, "x") == "2h"
    assert window_name_from_minutes(45, "x") == "45m"
    assert window_name_from_minutes(None, "fallback") == "fallback"


def test_summarize_quota_marks_exhausted_and_refuses_empty_answers():
    ok = summarize_quota([QuotaWindow(name="5h", remaining_percent=40.0)], source="s")
    assert ok.status == "ok" and ok.source == "s" and ok.fetched_at
    dry = summarize_quota([QuotaWindow(name="5h", remaining_percent=0.0)])
    assert dry.status == "exhausted"
    flagged = summarize_quota([], balance=QuotaBalance(amount="1"), exhausted=True)
    assert flagged.status == "exhausted"
    empty = summarize_quota([], detail="nothing here")
    assert empty.status == "unknown" and empty.detail == "nothing here"


def test_looks_like_quota_error():
    assert looks_like_quota_error("quota exceeded")
    assert looks_like_quota_error("HTTP 429 Rate limit reached")
    assert looks_like_quota_error("Insufficient Balance")
    assert not looks_like_quota_error("bridge_restarted")
    assert not looks_like_quota_error(None)


def test_proxy_map_prefers_upper_case_and_skips_blank():
    env = {"HTTPS_PROXY": "http://p:1", "https_proxy": "http://ignored", "http_proxy": " ", "HTTP_PROXY": ""}
    assert _proxy_map(env) == {"https": "http://p:1"}
    assert _proxy_map({}) == {}


# --- cache ---------------------------------------------------------------------


def test_quota_cache_expires_and_keeps_last_good():
    cache = QuotaCache(ttl_sec=10)
    good = QuotaStatus(status="ok", windows=[QuotaWindow(name="5h", remaining_percent=50)])
    cache.put("codex", good)
    now = time.monotonic()
    assert cache.get("codex", now=now) is good
    assert cache.get("codex", now=now + 11) is None
    assert cache.last_good("codex") is good
    cache.put("codex", unknown_quota("boom"))
    assert cache.get("codex").status == "unknown"
    assert cache.last_good("codex") is good
    cache.invalidate("codex")
    assert cache.get("codex") is None
    assert cache.last_good("codex") is good
    cache.clear()
    assert cache.last_good("codex") is None


def test_quota_cache_zero_ttl_never_serves_fresh():
    cache = QuotaCache(ttl_sec=0)
    cache.put("x", QuotaStatus(status="ok"))
    assert cache.get("x") is None
    assert cache.last_good("x") is not None


# --- fetch_quota --------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_quota_unknown_when_no_provider():
    cache = QuotaCache(60)
    row = await fetch_quota(_agent("mystery", "acp"), {}, cache=cache, timeout_sec=1, providers={})
    assert row["status"] == "unknown"
    assert "not supported for mystery" in row["detail"]
    assert row["cached"] is False
    again = await fetch_quota(_agent("mystery", "acp"), {}, cache=cache, timeout_sec=1, providers={})
    assert again["cached"] is True


@pytest.mark.asyncio
async def test_fetch_quota_serves_cache_and_marks_it():
    calls = 0

    async def provider(cfg, env):
        nonlocal calls
        calls += 1
        return QuotaStatus(status="ok", windows=[QuotaWindow(name="5h", remaining_percent=70)])

    cache = QuotaCache(60)
    cfg = _agent("kimi", "acp")
    first = await fetch_quota(cfg, {}, cache=cache, timeout_sec=1, providers={"kimi": provider})
    second = await fetch_quota(cfg, {}, cache=cache, timeout_sec=1, providers={"kimi": provider})
    assert calls == 1
    assert first["cached"] is False and second["cached"] is True
    assert second["windows"][0]["remaining_percent"] == 70
    assert first["fetched_at"]


@pytest.mark.asyncio
async def test_fetch_quota_timeout_is_unknown_and_bounded():
    async def slow(cfg, env):
        await asyncio.sleep(5)
        return QuotaStatus(status="ok")

    cache = QuotaCache(600)
    started = time.monotonic()
    row = await fetch_quota(_agent("grok", "acp"), {}, cache=cache, timeout_sec=0.2, providers={"grok": slow})
    assert time.monotonic() - started < 2
    assert row["status"] == "unknown"
    assert "timed out after 0.2s" in row["detail"]
    # Failures are memoised for a short spell only.
    assert cache.get("grok") is not None
    assert cache.get("grok", now=time.monotonic() + FAILURE_CACHE_SEC + 1) is None


@pytest.mark.asyncio
async def test_fetch_quota_provider_exception_falls_back_to_last_good():
    state = {"fail": False}

    async def flaky(cfg, env):
        if state["fail"]:
            raise RuntimeError("HTTP 429: Rate limited.")
        return QuotaStatus(status="ok", windows=[QuotaWindow(name="weekly", remaining_percent=61)])

    flaky.quota_source = "test source"
    cache = QuotaCache(600)
    cfg = _agent("claude", "acp")
    good = await fetch_quota(cfg, {}, cache=cache, timeout_sec=1, providers={"claude": flaky})
    assert good["status"] == "ok" and good["source"] == "test source"
    cache.invalidate("claude")
    state["fail"] = True
    stale = await fetch_quota(cfg, {}, cache=cache, timeout_sec=1, providers={"claude": flaky})
    assert stale["status"] == "ok"
    assert stale["stale"] is True and stale["cached"] is True
    assert "RuntimeError: HTTP 429" in stale["detail"]
    assert stale["windows"][0]["remaining_percent"] == 61


@pytest.mark.asyncio
async def test_fetch_quota_provider_exception_without_history_is_unknown():
    async def broken(cfg, env):
        raise ValueError("bad payload")

    row = await fetch_quota(_agent("dsh", "acp"), {}, cache=QuotaCache(60), timeout_sec=1, providers={"dsh": broken})
    assert row["status"] == "unknown"
    assert "ValueError: bad payload" in row["detail"]


@pytest.mark.asyncio
async def test_fetch_quota_normalises_bogus_status_and_fills_source():
    async def odd(cfg, env):
        return QuotaStatus(status="green", windows=[QuotaWindow(name="5h", remaining_percent=1)])

    odd.quota_source = "odd source"
    row = await fetch_quota(_agent("kimi", "acp"), {}, cache=QuotaCache(60), timeout_sec=1, providers={"kimi": odd})
    assert row["status"] == "unknown"
    assert row["source"] == "odd source"


def test_resolve_provider_prefers_name_then_protocol():
    table = {"kimi": "by-name", "protocol:codex": "by-protocol"}
    assert resolve_provider(_agent("kimi", "acp"), table) == "by-name"
    assert resolve_provider(_agent("codex-alt", "codex"), table) == "by-protocol"
    assert resolve_provider(_agent("cursor", "acp"), table) is None


def test_provider_table_gates_experimental_workers():
    plain = default_providers()
    assert set(plain) == {"protocol:codex", "kimi", "dsh"}
    full = default_providers(experimental=True)
    assert {"grok", "claude"} <= set(full)
    config = AppConfig(quota=QuotaConfig(experimental=False))
    table = provider_table(config)
    assert table["grok"] is not full["grok"]
    assert provider_table(AppConfig(quota=QuotaConfig(experimental=True)))["grok"] is full["grok"]


@pytest.mark.asyncio
async def test_experimental_placeholders_explain_the_flag():
    table = provider_table(AppConfig(quota=QuotaConfig(experimental=False)))
    grok = await table["grok"](_agent("grok", "acp"), {})
    claude = await table["claude"](_agent("claude", "acp"), {})
    for status in (grok, claude):
        assert status.status == "unknown"
        assert "experimental = true" in (status.detail or "")


# --- codex ---------------------------------------------------------------------


def test_parse_codex_rate_limits_windows_plan_and_credits():
    payload = {
        "rateLimits": {
            "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": FAR_FUTURE},
            "secondary": {"usedPercent": 2.5, "windowDurationMins": 10080, "resetsAt": FAR_FUTURE},
            "credits": {"hasCredits": True, "unlimited": False, "balance": "12.50"},
            "planType": "plus",
            "rateLimitReachedType": None,
        }
    }
    status = parse_codex_rate_limits(payload)
    assert status.status == "ok"
    assert [(w.name, w.remaining_percent) for w in status.windows] == [("5h", 90.0), ("weekly", 97.5)]
    assert status.windows[0].resets_at == "2100-01-01T00:00:00+00:00"
    assert status.windows[0].resets_in_sec is not None and status.windows[0].resets_in_sec > 0
    assert status.plan == "plus"
    assert status.balance == QuotaBalance(amount="12.50", currency="USD")


def test_parse_codex_rate_limits_reached_is_exhausted():
    payload = {"rateLimits": {"primary": {"usedPercent": 100}, "rateLimitReachedType": "primary"}}
    status = parse_codex_rate_limits(payload)
    assert status.status == "exhausted"
    assert "primary" in (status.detail or "")


def test_parse_codex_rate_limits_without_plan_is_unknown():
    assert parse_codex_rate_limits({}).status == "unknown"
    assert "API-key" in (parse_codex_rate_limits({}).detail or "")
    assert parse_codex_rate_limits(None).status == "unknown"
    unlimited = parse_codex_rate_limits({"rateLimits": {"credits": {"hasCredits": True, "unlimited": True}}})
    assert unlimited.status == "ok" and unlimited.balance == QuotaBalance(amount="unlimited")


def _codex_cfg() -> AgentConfig:
    return AgentConfig(name="codex", protocol="codex", command=["codex"])


def _patch_codex_command(monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.quota_codex.resolve_codex_command",
        lambda command, fallbacks=None, *, env=None: [sys.executable, str(FAKE_CODEX)],
    )


@pytest.mark.asyncio
async def test_codex_provider_reads_rate_limits_from_app_server(monkeypatch):
    _patch_codex_command(monkeypatch)
    monkeypatch.setattr("agent_bridge.quota_codex.POST_INIT_SETTLE_SEC", 0.0)
    status = await fetch_codex_quota(_codex_cfg(), {"PATH": "/usr/bin"})
    assert status.status == "ok"
    assert status.plan == "plus"
    assert [w.name for w in status.windows] == ["5h", "weekly"]
    assert status.windows[0].remaining_percent == 90.0


@pytest.mark.asyncio
async def test_codex_provider_reports_sign_in_errors(monkeypatch):
    _patch_codex_command(monkeypatch)
    monkeypatch.setattr("agent_bridge.quota_codex.POST_INIT_SETTLE_SEC", 0.0)
    env = {"FAKE_CODEX_APP_SERVER_ERROR": "token_invalidated"}
    with pytest.raises(RuntimeError, match="not signed in"):
        await fetch_codex_quota(_codex_cfg(), env)


@pytest.mark.asyncio
async def test_codex_provider_hang_becomes_unknown_through_fetch_quota(monkeypatch):
    _patch_codex_command(monkeypatch)
    env = {"FAKE_CODEX_APP_SERVER_HANG": "5"}
    started = time.monotonic()
    row = await fetch_quota(
        _codex_cfg(),
        env,
        cache=QuotaCache(60),
        timeout_sec=0.5,
        providers={"protocol:codex": fetch_codex_quota},
    )
    assert time.monotonic() - started < 4
    assert row["status"] == "unknown"
    assert "timed out" in row["detail"]


# --- kimi ----------------------------------------------------------------------


def test_kimi_usage_url_honours_base_url_override():
    assert kimi_usage_url({}) == "https://api.kimi.com/coding/v1/usages"
    assert kimi_usage_url({"KIMI_CODE_BASE_URL": "https://proxy.example/v1/"}) == "https://proxy.example/v1/usages"


def test_kimi_access_token_reads_credential_file(tmp_path: Path):
    creds = tmp_path / "credentials"
    creds.mkdir()
    token, problem = kimi_access_token(tmp_path)
    assert token is None and "kimi login" in problem
    (creds / "kimi-code.json").write_text(
        json.dumps({"access_token": "tok", "refresh_token": "r", "expires_at": FAR_FUTURE}),
        encoding="utf-8",
    )
    assert kimi_access_token(tmp_path) == ("tok", None)
    (creds / "kimi-code.json").write_text(json.dumps({"access_token": "tok", "expires_at": 1}), encoding="utf-8")
    token, problem = kimi_access_token(tmp_path, now=100.0)
    assert token is None and "expired" in problem
    (creds / "kimi-code.json").write_text("{not json", encoding="utf-8")
    token, problem = kimi_access_token(tmp_path)
    assert token is None and "JSONDecodeError" in problem


def test_parse_kimi_usage_reads_summary_and_limits():
    payload = {
        "usage": {"used": 30, "limit": 100, "reset_at": "2100-01-01T00:00:00.443553353Z"},
        "limits": [
            {"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}, "detail": {"remaining": 20, "limit": 80}},
            {"name": "Opus", "detail": {"used": 5, "limit": 5, "reset_in": 60}},
            {"detail": {"nothing": True}},
        ],
    }
    status = parse_kimi_usage(payload)
    assert status.status == "exhausted"  # the Opus limit is fully used
    assert [(w.name, w.remaining_percent) for w in status.windows] == [("weekly", 70.0), ("5h", 25.0), ("Opus", 0.0)]
    assert status.windows[0].resets_at == "2100-01-01T00:00:00.443553+00:00"
    assert status.windows[2].resets_in_sec is not None and 55 <= status.windows[2].resets_in_sec <= 60


def test_parse_kimi_usage_empty_is_unknown():
    assert parse_kimi_usage({}).status == "unknown"
    assert parse_kimi_usage(None).status == "unknown"


@pytest.mark.asyncio
async def test_kimi_provider_explains_api_key_sessions(tmp_path: Path):
    status = await fetch_kimi_quota(
        AgentConfig(name="kimi", protocol="acp", command=["kimi", "acp"]),
        {"KIMI_CODE_HOME": str(tmp_path), "MOONSHOT_API_KEY": "sk-x"},
    )
    assert status.status == "unknown"
    assert "Open Platform" in (status.detail or "")


@pytest.mark.asyncio
async def test_kimi_provider_sends_bearer_token(tmp_path: Path, monkeypatch):
    creds = tmp_path / "credentials"
    creds.mkdir()
    (creds / "kimi-code.json").write_text(json.dumps({"access_token": "tok"}), encoding="utf-8")
    seen = {}

    async def fake_get(url, *, headers=None, env=None, timeout=10.0):
        seen["url"] = url
        seen["headers"] = headers
        return {"usage": {"used": 1, "limit": 4}}

    monkeypatch.setattr("agent_bridge.quota_kimi.get_json", fake_get)
    status = await fetch_kimi_quota(
        AgentConfig(name="kimi", protocol="acp", command=["kimi", "acp"]),
        {"KIMI_CODE_HOME": str(tmp_path)},
    )
    assert seen["url"].endswith("/usages")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert status.status == "ok" and status.windows[0].remaining_percent == 75.0


# --- deepseek ------------------------------------------------------------------


def test_parse_deepseek_balance_prefers_non_zero_currency():
    payload = {
        "is_available": True,
        "balance_infos": [
            {"currency": "USD", "total_balance": "0.00"},
            {"currency": "CNY", "total_balance": "110.00"},
        ],
    }
    status = parse_deepseek_balance(payload, provider="deepseek")
    assert status.status == "ok"
    assert status.balance == QuotaBalance(amount="110.00", currency="CNY")
    assert status.windows == []
    assert "deepseek" in (status.detail or "")


def test_parse_deepseek_balance_unavailable_is_exhausted():
    status = parse_deepseek_balance({"is_available": False, "balance_infos": [{"currency": "CNY", "total_balance": "0"}]})
    assert status.status == "exhausted"
    assert parse_deepseek_balance({"is_available": True}).status == "unknown"
    assert parse_deepseek_balance(None).status == "unknown"


@pytest.mark.asyncio
async def test_dsh_provider_needs_the_official_key(monkeypatch):
    monkeypatch.setattr("agent_bridge.quota_deepseek.default_model", lambda env=None: ("acme", "large"))
    status = await fetch_dsh_quota(AgentConfig(name="dsh", protocol="acp", command=["dsh-acp-demo"]), {})
    assert status.status == "unknown"
    assert "DEEPSEEK_API_KEY" in (status.detail or "")
    assert "acme" in (status.detail or "")


@pytest.mark.asyncio
async def test_dsh_provider_queries_balance(monkeypatch):
    monkeypatch.setattr("agent_bridge.quota_deepseek.default_model", lambda env=None: None)
    seen = {}

    async def fake_get(url, *, headers=None, env=None, timeout=10.0):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        return {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "9.5"}]}

    monkeypatch.setattr("agent_bridge.quota_deepseek.get_json", fake_get)
    status = await fetch_dsh_quota(
        AgentConfig(name="dsh", protocol="acp", command=["dsh-acp-demo"]), {"DEEPSEEK_API_KEY": "sk-ds"}
    )
    assert seen["url"] == "https://api.deepseek.com/user/balance"
    assert seen["auth"] == "Bearer sk-ds"
    assert status.status == "ok" and status.balance.amount == "9.5"


# --- grok ----------------------------------------------------------------------


def test_grok_auth_token_prefers_oidc_scope_and_checks_expiry(tmp_path: Path):
    token, problem = grok_auth_token(tmp_path)
    assert token is None and "grok login" in problem
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "https://accounts.x.ai/sign-in": {"key": "legacy"},
                "https://auth.x.ai::client": {"key": "oidc", "expires_at": FAR_FUTURE},
            }
        ),
        encoding="utf-8",
    )
    assert grok_auth_token(tmp_path) == ("oidc", None)
    (tmp_path / "auth.json").write_text(
        json.dumps({"https://auth.x.ai::client": {"key": "oidc", "expires_at": 1000}}), encoding="utf-8"
    )
    token, problem = grok_auth_token(tmp_path, now=2000.0)
    assert token is None and "expired" in problem


def test_parse_grok_billing_weekly_window_and_products():
    payload = {
        "config": {
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "x", "end": "2100-01-01T00:00:00Z"},
            "creditUsagePercent": 46.0,
            "productUsage": [{"product": "GrokBuild", "usagePercent": 41.0}],
        }
    }
    status = parse_grok_billing(payload)
    assert status.status == "ok"
    assert status.windows[0].name == "weekly"
    assert status.windows[0].remaining_percent == 54.0
    assert status.windows[0].resets_at == "2100-01-01T00:00:00+00:00"
    assert "GrokBuild 41% used" in (status.detail or "")
    assert parse_grok_billing({"config": {}}).status == "unknown"
    assert parse_grok_billing({"config": {"creditUsagePercent": 100}}).status == "exhausted"


@pytest.mark.asyncio
async def test_grok_provider_explains_api_key_sessions(tmp_path: Path):
    status = await fetch_grok_quota(
        AgentConfig(name="grok", protocol="acp", command=["grok"]),
        {"GROK_HOME": str(tmp_path), "XAI_API_KEY": "xai-x"},
    )
    assert status.status == "unknown"
    assert "API-key" in (status.detail or "")


# --- claude --------------------------------------------------------------------


def test_claude_access_token_reads_oauth_block(tmp_path: Path):
    token, problem = claude_access_token(tmp_path)
    assert token is None and "claude auth login" in problem
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": FAR_FUTURE * 1000}}), encoding="utf-8"
    )
    assert claude_access_token(tmp_path) == ("tok", None)
    # Claude stores expiresAt in epoch milliseconds.
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": 1_700_000_000_000}}), encoding="utf-8"
    )
    token, problem = claude_access_token(tmp_path, now=1_800_000_000.0)
    assert token is None and "expired" in problem


def test_parse_claude_usage_windows():
    payload = {
        "five_hour": {"utilization": 74.0, "resets_at": "2100-01-01T00:00:00Z"},
        "seven_day": {"utilization": 10.0, "resets_at": FAR_FUTURE},
        "seven_day_opus": {"utilization": 100.0, "resets_at": None},
    }
    status = parse_claude_usage(payload)
    assert status.status == "exhausted"
    assert [(w.name, w.remaining_percent) for w in status.windows] == [
        ("5h", 26.0),
        ("weekly", 90.0),
        ("weekly:opus", 0.0),
    ]
    assert status.windows[0].resets_at == status.windows[1].resets_at == "2100-01-01T00:00:00+00:00"
    assert parse_claude_usage({}).status == "unknown"


@pytest.mark.asyncio
async def test_claude_provider_only_for_oauth_logins(tmp_path: Path):
    status = await fetch_claude_quota(
        AgentConfig(name="claude", protocol="acp", command=["claude-agent-acp"]),
        {"CLAUDE_CONFIG_DIR": str(tmp_path), "ANTHROPIC_API_KEY": "sk-ant"},
    )
    assert status.status == "unknown"
    assert "auth=api-key" in (status.detail or "")


@pytest.mark.asyncio
async def test_claude_provider_sends_oauth_headers(tmp_path: Path, monkeypatch):
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok"}}), encoding="utf-8"
    )
    seen = {}

    async def fake_get(url, *, headers=None, env=None, timeout=10.0):
        seen["url"] = url
        seen["headers"] = headers
        return {"five_hour": {"utilization": 50, "resets_at": FAR_FUTURE}}

    monkeypatch.setattr("agent_bridge.quota_claude.get_json", fake_get)
    status = await fetch_claude_quota(
        AgentConfig(name="claude", protocol="acp", command=["claude-agent-acp"]),
        {"CLAUDE_CONFIG_DIR": str(tmp_path)},
    )
    assert seen["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert seen["headers"]["User-Agent"].startswith("claude-code/")
    assert status.status == "ok" and status.windows[0].remaining_percent == 50.0


# --- config --------------------------------------------------------------------


def test_quota_config_defaults_and_overlay(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert cfg.quota == QuotaConfig(enabled=True, timeout_sec=4.0, cache_sec=300.0, experimental=False)
    assert cfg.warnings == []
    (tmp_path / "agents.toml").write_text(
        "[quota]\nenabled = false\ntimeout_sec = 1.5\ncache_sec = 0\nexperimental = true\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.quota == QuotaConfig(enabled=False, timeout_sec=1.5, cache_sec=0.0, experimental=True)
    assert cfg.warnings == []


# --- describe_quotas (CLI payload) --------------------------------------------


@pytest.mark.asyncio
async def test_describe_quotas_covers_every_agent(monkeypatch):
    config = AppConfig(
        agents={
            "fake": AgentConfig(name="fake", protocol="fake", command=["fake"]),
            "ghost": AgentConfig(name="ghost", protocol="acp", command=["definitely-not-installed-xyz"]),
        }
    )
    monkeypatch.setattr("agent_bridge.quota.QuotaCache", QuotaCache)
    rows = await describe_quotas(config)
    assert set(rows) == {"fake", "ghost"}
    assert rows["fake"]["status"] == "unknown" and "not supported" in rows["fake"]["detail"]
    assert rows["ghost"]["status"] == "unknown" and "not found" in rows["ghost"]["detail"]


def test_far_future_fixture_is_actually_in_the_future():
    assert datetime.fromtimestamp(FAR_FUTURE, tz=UTC) > datetime.now(UTC) + timedelta(days=365)
