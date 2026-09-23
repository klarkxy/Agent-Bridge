from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from agent_bridge.adapters.acp import AcpAdapter, AcpStageError, RpcTimeoutError, _Live
from agent_bridge.config import AgentConfig
from agent_bridge.models import Session, Task

CURSOR_AGENT = Path(__file__).with_name("cursor_agent.py")


def _adapter(tmp_path: Path, command: list[str] | None = None) -> AcpAdapter:
    return AcpAdapter(
        AgentConfig(
            name="cursor",
            protocol="acp",
            command=command or [sys.executable, str(CURSOR_AGENT), "acp"],
        ),
        tmp_path / "bridge-home",
    )


def _session(tmp_path: Path) -> Session:
    return Session(session_id="diagnostic", agent="cursor", cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_process_start_failure_is_pre_prompt(tmp_path):
    adapter = _adapter(tmp_path, [str(tmp_path / "missing-worker.exe"), "acp"])
    with pytest.raises(AcpStageError) as caught:
        await adapter.ensure_session(_session(tmp_path))
    error = caught.value
    assert error.stage == "process_start"
    assert error.category == "process_start"
    assert error.worker_exit_code is None
    assert error.prompt_sent is False
    assert str(error)


@pytest.mark.asyncio
async def test_model_discovery_exit_has_allowlisted_stderr_only(tmp_path):
    adapter = _adapter(tmp_path)
    with pytest.raises(AcpStageError) as caught:
        await adapter._cursor_models(
            [sys.executable, "-c", "import sys; print('secret=123 connection closed', file=sys.stderr); sys.exit(7)", "acp"],
            adapter._env(),
            str(tmp_path),
        )
    error = caught.value
    assert error.stage == "model_discovery"
    assert error.category == "worker_exit"
    assert error.worker_exit_code == 7
    assert error.stderr_summary == "connection closed"
    assert error.prompt_sent is False
    assert "secret" not in str(error) + error.stderr_summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rpc", "stage"),
    [
        ("initialize", "initialize"),
        ("session/new", "session/new"),
        ("session/resume", "session/resume"),
        ("session/load", "session/load"),
        ("session/set_config_option model", "model_selection"),
    ],
)
async def test_handshake_failure_keeps_text_and_safe_context(tmp_path, rpc, stage):
    adapter = _adapter(tmp_path)
    session = _session(tmp_path)
    live = _Live()
    live.stderr_tail = "token=private Internal error"
    adapter._live[session.session_id] = live

    async def failed_rpc():
        raise RuntimeError("connection closed")

    with pytest.raises(AcpStageError) as caught:
        await adapter._rpc(failed_rpc(), rpc, session)
    error = caught.value
    assert str(error) == "connection closed"
    assert error.stage == stage
    assert error.category == ("selection_error" if stage == "model_selection" else "protocol_error")
    assert error.stderr_summary == "internal error"
    assert error.prompt_sent is False


@pytest.mark.asyncio
async def test_handshake_timeout_retains_timeout_type_and_stage(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    session = _session(tmp_path)
    monkeypatch.setattr(adapter, "shutdown", lambda _session: asyncio.sleep(0))
    with pytest.raises(RpcTimeoutError) as caught:
        await adapter._rpc(asyncio.sleep(30), "session/new", session, timeout=0.01)
    error = caught.value
    assert str(error) == "cursor session/new timed out after 0s"
    assert error.stage == "session/new"
    assert error.category == "timeout"
    assert error.prompt_sent is False


@pytest.mark.asyncio
async def test_prompt_failure_is_marked_sent_after_call_starts(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    session = _session(tmp_path)
    await adapter.ensure_session(session)
    live = adapter._live[session.session_id]

    async def failed_prompt(**_kwargs):
        raise RuntimeError("connection closed")

    monkeypatch.setattr(live.conn, "prompt", failed_prompt)
    task = Task(
        task_id="diagnostic-prompt",
        session_id=session.session_id,
        agent="cursor",
        cwd=session.cwd,
        message="test",
    )
    try:
        with pytest.raises(AcpStageError) as caught:
            await adapter.run_turn(session, task)
        error = caught.value
        assert str(error) == "connection closed"
        assert error.stage == "prompt"
        assert error.prompt_sent is True
    finally:
        await adapter.shutdown(session)


@pytest.mark.asyncio
async def test_synchronous_prompt_failure_reports_uncertain_delivery(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    session = _session(tmp_path)
    await adapter.ensure_session(session)
    live = adapter._live[session.session_id]

    def failed_prompt(**_kwargs):
        raise RuntimeError("prompt setup failed")

    monkeypatch.setattr(live.conn, "prompt", failed_prompt)
    task = Task(
        task_id="diagnostic-prompt-sync",
        session_id=session.session_id,
        agent="cursor",
        cwd=session.cwd,
        message="test",
    )
    try:
        with pytest.raises(AcpStageError) as caught:
            await adapter.run_turn(session, task)
        assert caught.value.stage == "prompt"
        assert caught.value.prompt_sent is None
    finally:
        await adapter.shutdown(session)
