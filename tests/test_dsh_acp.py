from __future__ import annotations

from pathlib import Path

import pytest

from agent_bridge.adapters.acp import AcpAdapter, _Live, dsh_native_model_value
from agent_bridge.config import AgentConfig
from agent_bridge.models import Session

# Shape from dsh-acp model-control.ts: model values are '["provider","model"]'
# JSON, options arrive grouped by provider, and reasoning_effort lists the
# current model's effort ids plus "" for the provider default.
MODEL_OPTION = {
    "id": "model",
    "currentValue": '["deepseek-official","deepseek-v4-flash"]',
    "options": [
        {
            "group": "deepseek-official",
            "name": "DeepSeek",
            "options": [
                {
                    "value": '["deepseek-official","deepseek-v4-flash"]',
                    "name": "deepseek-v4-flash",
                },
                {
                    "value": '["deepseek-official","deepseek-v4-pro"]',
                    "name": "deepseek-v4-pro",
                },
            ],
        },
        {
            "group": "opencode",
            "name": "OpenCode",
            "options": [
                {"value": '["opencode","mimo-v2.5-free"]', "name": "mimo-v2.5-free"},
            ],
        },
    ],
}
EFFORT_OPTION = {
    "id": "reasoning_effort",
    "currentValue": "",
    "options": [
        {"value": "", "name": "Provider default"},
        {"value": "low", "name": "low"},
        {"value": "high", "name": "high"},
        {"value": "max", "name": "max"},
    ],
}


class FakeResponse:
    def __init__(self, config_options=None, session_id=None):
        self.config_options = config_options
        self.session_id = session_id


class FakeConn:
    def __init__(self, config_options=None, reject_ids=()):
        self.calls: list[tuple] = []
        self.config_options = (
            config_options if config_options is not None else [MODEL_OPTION, EFFORT_OPTION]
        )
        self.reject_ids = set(reject_ids)

    async def set_config_option(self, config_id, session_id, value):
        self.calls.append((config_id, value))
        if config_id in self.reject_ids:
            raise RuntimeError(f"{config_id} rejected")
        return FakeResponse(config_options=self.config_options)

    async def resume_session(self, session_id, cwd, mcp_servers=None):
        self.calls.append(("resume", session_id))
        return FakeResponse(config_options=self.config_options)

    async def load_session(self, cwd, session_id, mcp_servers=None, noReplay=None):
        self.calls.append(("load", session_id))
        return FakeResponse(config_options=self.config_options)


def build(conn=None, session_kwargs=None, native=True, env_model=None):
    adapter = AcpAdapter(
        AgentConfig(name="dsh", protocol="acp", command=["dsh-acp-demo"]),
        Path("."),
    )
    live = _Live()
    live.conn = conn or FakeConn()
    live.config_options = [MODEL_OPTION, EFFORT_OPTION]
    live.dsh_native_acp = native
    live.dsh_env_model = env_model
    session = Session(
        session_id="sess_dsh",
        agent="dsh",
        cwd=".",
        native_session_id="dsh-native-1",
        **(session_kwargs or {}),
    )
    return adapter, live, session


def test_model_value_maps_provider_model():
    options = [MODEL_OPTION]
    assert (
        dsh_native_model_value("deepseek-official/deepseek-v4-pro", options)
        == '["deepseek-official","deepseek-v4-pro"]'
    )


def test_model_value_maps_bare_model_to_any_provider():
    options = [MODEL_OPTION]
    assert (
        dsh_native_model_value("mimo-v2.5-free", options)
        == '["opencode","mimo-v2.5-free"]'
    )


def test_model_value_rejects_unknown_model_and_provider():
    options = [MODEL_OPTION]
    assert dsh_native_model_value("no-such-model", options) is None
    assert dsh_native_model_value("other/deepseek-v4-pro", options) is None


@pytest.mark.asyncio
async def test_native_model_is_applied_through_the_config_option():
    adapter, live, session = build(
        session_kwargs={"model": "deepseek-official/deepseek-v4-pro"}
    )
    await adapter._sync_dsh_selection(live, session)
    assert ("model", '["deepseek-official","deepseek-v4-pro"]') in live.conn.calls
    assert live.applied_model == "deepseek-official/deepseek-v4-pro"


@pytest.mark.asyncio
async def test_native_model_already_current_is_not_resent():
    adapter, live, session = build(session_kwargs={"model": "deepseek-v4-flash"})
    await adapter._sync_dsh_selection(live, session)
    assert not any(call[0] == "model" for call in live.conn.calls)
    assert live.applied_model == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_native_unadvertised_model_fails_and_names_the_real_options():
    adapter, live, session = build(session_kwargs={"model": "nope"})
    with pytest.raises(RuntimeError, match=r"dsh model 'nope'.*deepseek-official/deepseek-v4-flash"):
        await adapter._sync_dsh_selection(live, session)


@pytest.mark.asyncio
async def test_native_settings_default_model_is_applied_without_a_request():
    adapter, live, session = build(env_model="opencode/mimo-v2.5-free")
    await adapter._sync_dsh_selection(live, session)
    assert ("model", '["opencode","mimo-v2.5-free"]') in live.conn.calls
    assert live.applied_model == "opencode/mimo-v2.5-free"


@pytest.mark.asyncio
async def test_native_unadvertised_settings_default_warns_once():
    adapter, live, session = build(env_model="missing/provider-model")
    await adapter._sync_dsh_selection(live, session)
    await adapter._sync_dsh_selection(live, session)
    assert not any(call[0] == "model" for call in live.conn.calls)
    assert sum("missing/provider-model" in item for item in live.pending_warnings) == 1
    assert live.applied_model == "deepseek-official/deepseek-v4-flash"


@pytest.mark.asyncio
async def test_native_effort_is_applied_through_reasoning_effort():
    adapter, live, session = build(session_kwargs={"effort": "high"})
    await adapter._sync_dsh_selection(live, session)
    assert ("reasoning_effort", "high") in live.conn.calls
    assert live.applied_effort == "high"


@pytest.mark.asyncio
async def test_native_unadvertised_effort_warns_once():
    adapter, live, session = build(session_kwargs={"effort": "off"})
    await adapter._sync_dsh_selection(live, session)
    await adapter._sync_dsh_selection(live, session)
    assert not any(call[0] == "reasoning_effort" for call in live.conn.calls)
    assert sum("effort=off" in item for item in live.pending_warnings) == 1
    assert live.applied_effort == "off"


@pytest.mark.asyncio
async def test_native_without_a_request_observes_the_current_selection():
    adapter, live, session = build()
    await adapter._sync_dsh_selection(live, session)
    assert live.conn.calls == []
    assert live.applied_model == "deepseek-official/deepseek-v4-flash"
    assert live.applied_effort is None


@pytest.mark.asyncio
async def test_legacy_demo_path_is_untouched_by_the_native_sync():
    adapter, live, session = build(
        native=False, session_kwargs={"model": "deepseek-v4-pro", "effort": "high"}
    )
    await adapter._sync_dsh_selection(live, session)
    assert live.conn.calls == []
    assert live.pending_warnings == []


@pytest.mark.asyncio
async def test_native_revive_uses_resume_not_load():
    adapter, live, _session = build()
    await adapter._call_load_session(live.conn, ".", "dsh-native-1", use_resume=True)
    assert live.conn.calls == [("resume", "dsh-native-1")]
