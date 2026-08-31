from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from phone_agent_gateway.ai_bridge.local_control import (
    LocalControlError,
    load_or_create_control_token,
)
from phone_agent_gateway.ai_bridge.production_security import AuditLedger
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer


def _config() -> ProviderConfig:
    return ProviderConfig(
        stt_provider="parakeet_local",
        llm_provider="ollama",
        tts_provider="edge_tts",
        tts_voice_id="en-US-AndrewMultilingualNeural",
        stt_language="en-US",
    )


def test_control_token_is_private_stable_and_rejects_symlink(tmp_path: Path) -> None:
    token_path = tmp_path / "private" / "control.token"
    first = load_or_create_control_token(token_path)
    second = load_or_create_control_token(token_path)
    assert first == second
    assert len(first) >= 32
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600

    linked = tmp_path / "linked.token"
    linked.symlink_to(token_path)
    with pytest.raises(LocalControlError, match="non-symlink"):
        load_or_create_control_token(linked)

    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    with pytest.raises(LocalControlError, match="group/world writable"):
        load_or_create_control_token(unsafe_parent / "control.token")


@pytest.mark.asyncio
async def test_mcp_dial_executes_autonomously_and_still_redacts_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP dialing is autonomous as of c7a009b.

    The one-time operator approval this test previously asserted was removed
    deliberately ("Enable 100% autonomous autopilot execution for MCP and
    tools"), so /api/mcp/dial/request now dials directly. What must still hold
    is that the endpoint stays authenticated, the number is normalised before
    it reaches the dialer, and the raw destination never lands in the audit
    ledger.
    """

    token_path = tmp_path / "control.token"
    monkeypatch.setenv("PHONE_AGENT_CONTROL_TOKEN_FILE", str(token_path))
    server = PhoneAgentWebServer(
        config=_config(), audit_ledger=AuditLedger(tmp_path / "audit.jsonl")
    )

    async def ready() -> None:
        return None

    completed: list[tuple[str, bool]] = []

    async def fake_execute(number: str, *, recording_consent: bool) -> None:
        completed.append((number, recording_consent))

    server._gateway_preflight = ready  # type: ignore[method-assign]
    server._execute_dial = fake_execute  # type: ignore[method-assign]
    token = token_path.read_text().strip()
    headers = {"Authorization": f"Bearer {token}"}
    async with TestClient(TestServer(server.app)) as client:
        assert (await client.get("/api/mcp/status")).status == 401
        assert (await client.get("/api/mcp/status", headers=headers)).status == 200

        # Unauthenticated callers still cannot dial.
        assert (
            await client.post(
                "/api/mcp/dial/request",
                json={"destination": "00212600454425", "recording_consent": True},
            )
        ).status == 401

        requested = await client.post(
            "/api/mcp/dial/request",
            headers=headers,
            json={"destination": "00212600454425", "recording_consent": True},
        )
        assert requested.status == 200
        await asyncio.sleep(0)

    assert completed == [("+212600454425", True)]
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "00212600454425" not in audit, "the raw destination must never be logged"
    assert "dial_allowed" in audit


@pytest.mark.asyncio
async def test_stdio_mcp_server_negotiates_and_reads_live_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "control.token"
    monkeypatch.setenv("PHONE_AGENT_CONTROL_TOKEN_FILE", str(token_path))
    server = PhoneAgentWebServer(
        config=_config(), audit_ledger=AuditLedger(tmp_path / "audit.jsonl")
    )
    async with TestClient(TestServer(server.app)) as client:
        environment = os.environ.copy()
        environment["PHONE_AGENT_CONTROL_TOKEN_FILE"] = str(token_path)
        environment["PHONE_AGENT_WEB_URL"] = str(client.make_url("/")).rstrip("/")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "phone_agent_gateway.ai_bridge.mcp_server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        assert process.stdin is not None and process.stdout is not None

        async def request(request_id: int, method: str, params: dict) -> dict:
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            process.stdin.write(json.dumps(payload).encode() + b"\n")
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
            return json.loads(line)

        initialized = await request(
            1,
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}},
        )
        assert initialized["result"]["serverInfo"]["name"] == "phone-agent-local"
        listed = await request(2, "tools/list", {})
        assert {tool["name"] for tool in listed["result"]["tools"]} == {
            "phone_agent_status",
            "phone_agent_capabilities",
            "phone_agent_identity",
            "phone_agent_request_dial",
            "phone_agent_execute_approved_dial",
            "phone_agent_control_schema",
            "phone_agent_get_active_package",
            "phone_agent_validate_package",
            "phone_agent_stage_package",
            "phone_agent_list_deployments",
            "phone_agent_activate_deployment",
            "phone_agent_rollback_deployment",
            "phone_agent_recent_events",
            "phone_agent_list_tasks",
            "phone_agent_dial",
            "phone_agent_hangup",
            # Full configuration control: an external agent that can only dial
            # cannot actually run the appliance.
            "phone_agent_get_configuration",
            "phone_agent_set_configuration",
            "phone_agent_get_tool_control",
            "phone_agent_set_tool_control",
            "phone_agent_get_persona",
            "phone_agent_set_persona",
            "phone_agent_set_task",
            "phone_agent_delete_task",
            "phone_agent_get_integration",
            "phone_agent_set_integration",
            "phone_agent_test_integration",
            "phone_agent_set_remote_link",
            "phone_agent_pairing_code",
            "phone_agent_list_approvals",
            "phone_agent_decide_approval",
            "phone_agent_get_evaluation",
            "phone_agent_get_caller_memory",
        }
        resources = await request(20, "resources/list", {})
        assert {item["uri"] for item in resources["result"]["resources"]} == {
            "phoneagent://schema/agent-package",
            "phoneagent://state/active-package",
            "phoneagent://state/capabilities",
        }
        status = await request(
            3,
            "tools/call",
            {"name": "phone_agent_status", "arguments": {}},
        )
        assert status["result"]["structuredContent"]["call_state"] == "IDLE"
        identity = await request(
            4,
            "tools/call",
            {"name": "phone_agent_identity", "arguments": {}},
        )
        assert identity["result"]["structuredContent"]["identity_id"]
        assert identity["result"]["structuredContent"]["evaluation_passed"] is True
        process.stdin.close()
        await asyncio.wait_for(process.wait(), timeout=5)
        assert process.returncode == 0
