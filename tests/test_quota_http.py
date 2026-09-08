from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from agent_bridge.quota import QuotaHTTPError, get_json

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("route", ["direct", "proxy", "bypass"])
@pytest.mark.parametrize("status", [200, 429])
async def test_quota_http_preserves_headers_proxy_and_errors(route, status):
    received = asyncio.get_running_loop().create_future()
    body = b'{"remaining":83}' if status == 200 else b"limit\n" + b"x" * 200

    async def handle(reader, writer):
        try:
            received.set_result(await reader.readuntil(b"\r\n\r\n"))
            writer.write(f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        endpoint = f"http://127.0.0.1:{port}"
        url = "http://quota.invalid/usages" if route == "proxy" else endpoint + "/usages"
        env = {}
        if route == "proxy":
            env = {"HTTP_PROXY": endpoint, "HTTPS_PROXY": "http://127.0.0.1:1"}
        elif route == "bypass":
            env = {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": "127.0.0.1"}
        if status == 200:
            assert await get_json(url, headers={"Authorization": "Bearer test"}, env=env) == {"remaining": 83}
        else:
            with pytest.raises(QuotaHTTPError) as error:
                await get_json(url, headers={"Authorization": "Bearer test"}, env=env)
            assert error.value.status == 429
            assert str(error.value) == "HTTP 429: " + body.decode().strip().replace("\n", " ")[:160]
        request = await asyncio.wait_for(received, 1)
        assert b"accept: application/json" in request.lower()
        assert b"authorization: bearer test" in request.lower()
        target = url if route == "proxy" else "/usages"
        assert request.startswith(f"GET {target} HTTP/1.1\r\n".encode())


@pytest.mark.parametrize("stall_stage", ["headers", "body"])
async def test_quota_cli_deadline_closes_http_and_exits(tmp_path, stall_stage):
    """Exercise the real CLI/provider in a child, including asyncio.run shutdown."""
    connected = asyncio.Event()
    disconnected = asyncio.Event()

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            connected.set()
            if stall_stage == "body":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n{")
                await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            disconnected.set()

    home = tmp_path / "kimi"
    (home / "credentials").mkdir(parents=True)
    (home / "credentials" / "kimi-code.json").write_text(
        json.dumps({"access_token": "test-local-only", "expires_at": time.time() + 3600}), encoding="utf-8",
    )
    # Supply an isolated one-worker config; the CLI, provider, HTTP and shutdown
    # paths are unchanged. No real credential or external endpoint is used.
    script = """
import sys
import agent_bridge.config as config
from agent_bridge.cli import main
c = config.AppConfig(
    agents={'kimi': config.AgentConfig(name='kimi', protocol='acp', command=[sys.executable], env={
        'KIMI_CODE_HOME': sys.argv[1], 'KIMI_CODE_BASE_URL': sys.argv[2],
        'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'http_proxy': '', 'https_proxy': '', 'NO_PROXY': '*',
    })},
    env=config.EnvConfig(inherit=[], discover_proxy=False),
    quota=config.QuotaConfig(timeout_sec=1.5),
)
config.load_config = lambda: c
main(['--quota'])
"""
    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", "-c", script, str(home), f"http://127.0.0.1:{port}",
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 5)
            assert proc.returncode == 0, stderr.decode()
            quota = json.loads(stdout)["kimi"]
            assert quota["status"] == "unknown"
            assert "timed out after 1.5s" in quota["detail"]
            assert connected.is_set(), "the test must reach real HTTP I/O before its deadline"
            await asyncio.wait_for(disconnected.wait(), 1)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
