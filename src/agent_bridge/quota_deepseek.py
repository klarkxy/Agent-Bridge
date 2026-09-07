"""DeepSeek API balance for the DSH (DeepSeek Harness) worker.

DSH is pay-as-you-go against the DeepSeek Open Platform, so there is no
rolling window to report — only a balance and an ``is_available`` flag from
the documented ``GET /user/balance``. A ``$DSH_HOME`` that points at another
gateway has nothing Bridge can read; that case answers ``unknown``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_bridge.config import AgentConfig
from agent_bridge.dsh_home import default_model
from agent_bridge.quota import (
    QuotaBalance,
    QuotaStatus,
    as_float,
    get_json,
    summarize_quota,
    unknown_quota,
)

SOURCE = "deepseek GET /user/balance"
DEFAULT_BASE_URL = "https://api.deepseek.com"


def deepseek_balance_url(env: Mapping[str, str]) -> str:
    base = (env.get("DEEPSEEK_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    return f"{base.rstrip('/')}/user/balance"


def _pick_balance(infos: Sequence[Any]) -> QuotaBalance | None:
    chosen: Mapping[str, Any] | None = None
    for info in infos:
        if not isinstance(info, Mapping):
            continue
        if chosen is None:
            chosen = info
        # Prefer a non-zero balance when the account holds several currencies.
        total = as_float(info.get("total_balance"))
        if total is not None and total > 0 and (as_float(chosen.get("total_balance")) or 0) <= 0:
            chosen = info
    if chosen is None:
        return None
    amount = chosen.get("total_balance")
    if amount is None:
        return None
    currency = chosen.get("currency")
    return QuotaBalance(amount=str(amount), currency=str(currency) if currency else None)


def parse_deepseek_balance(payload: Mapping[str, Any] | None, *, provider: str | None = None) -> QuotaStatus:
    if not isinstance(payload, Mapping):
        return unknown_quota("deepseek returned no balance payload", source=SOURCE)
    infos = payload.get("balance_infos")
    balance = _pick_balance(infos) if isinstance(infos, Sequence) and not isinstance(infos, str) else None
    available = payload.get("is_available")
    blocked = available is False
    notes = []
    if provider:
        notes.append(f"dsh default provider {provider}")
    if blocked:
        notes.append("deepseek reports the account is not available for API calls")
    if balance is None and not blocked:
        return unknown_quota("deepseek balance payload had no balance_infos", source=SOURCE)
    return summarize_quota(
        [],
        balance=balance or QuotaBalance(amount="0"),
        source=SOURCE,
        detail="; ".join(notes) or None,
        exhausted=blocked,
    )


async def fetch_dsh_quota(cfg: AgentConfig, env: Mapping[str, str]) -> QuotaStatus:
    key = (env.get("DEEPSEEK_API_KEY") or "").strip()
    selection = default_model(env=env)
    provider = selection[0] if selection else None
    if not key:
        hint = f" (dsh default provider {provider})" if provider else ""
        return unknown_quota(
            f"DEEPSEEK_API_KEY is not set; the balance query needs the official DeepSeek API key{hint}",
            source=SOURCE,
        )
    payload = await get_json(
        deepseek_balance_url(env),
        headers={"Authorization": f"Bearer {key}"},
        env=env,
        timeout=10.0,
    )
    return parse_deepseek_balance(payload, provider=provider)


fetch_dsh_quota.quota_source = SOURCE  # type: ignore[attr-defined]
