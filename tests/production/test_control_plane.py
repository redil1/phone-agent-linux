from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from phone_agent_gateway.ai_bridge.control_plane import ControlPlaneStore
from phone_agent_gateway.ai_bridge.frappe_integration import FrappeConfigStore
from phone_agent_gateway.ai_bridge.openwa_integration import OpenWAConfigStore
from phone_agent_gateway.ai_bridge.personality.persona_compiler import (
    DEFAULT_EXAMPLES_PATH,
    DEFAULT_PERSONA_PATH,
    PersonaCompiler,
)
from phone_agent_gateway.ai_bridge.production_security import AuditLedger
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine
from phone_agent_gateway.ai_bridge.tool_control import ToolControlStore
from phone_agent_gateway.ai_bridge.web_research import WebResearchConfigStore
from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer


def _server(tmp_path: Path) -> PhoneAgentWebServer:
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_bytes(DEFAULT_PERSONA_PATH.read_bytes())
    compiler = PersonaCompiler(
        persona_path=persona_path,
        examples_path=DEFAULT_EXAMPLES_PATH,
    )
    return PhoneAgentWebServer(
        config=ProviderConfig(
            pipeline_mode="s2s_chatgpt_realtime",
            call_channel="gsm",
            stt_provider="parakeet_local",
            llm_provider="ollama",
            tts_provider="edge_tts",
            tts_voice_id="en-US-AndrewMultilingualNeural",
            stt_language="en-US",
        ),
        persona_compiler=compiler,
        task_engine=TaskEngine(user_contracts_dir=tmp_path / "tasks"),
        settings_path=tmp_path / "studio.json",
        audit_ledger=AuditLedger(tmp_path / "audit.jsonl"),
        tool_control_store=ToolControlStore(tmp_path / "tools.json"),
        openwa_config_store=OpenWAConfigStore(tmp_path / "openwa.json"),
        web_research_config_store=WebResearchConfigStore(tmp_path / "research.json"),
        frappe_config_store=FrappeConfigStore(tmp_path / "frappe.json"),
        control_plane_store=ControlPlaneStore(tmp_path / "control-plane"),
    )


@pytest.mark.asyncio
async def test_complete_agent_package_stages_and_activates_atomically(tmp_path: Path) -> None:
    server = _server(tmp_path)
    headers = {"Authorization": f"Bearer {server._control_token}"}
    async with TestClient(TestServer(server.app)) as client:
        assert (await client.get("/api/control/schema")).status == 401
        schema_response = await client.get("/api/control/schema", headers=headers)
        schema = await schema_response.json()
        assert schema_response.status == 200
        assert schema["schema"]["title"] == "AgentPackage"
        assert "gsm_media" in schema["immutable_boundaries"]

        current_response = await client.get("/api/control/package", headers=headers)
        current = await current_response.json()
        package = current["package"]
        package["package_id"] = "support_specialist"
        package["display_name"] = "Support specialist"
        package["objective"] = "Resolve customer support calls accurately and naturally."
        package["runtime"]["system_prompt"] = "Prioritize the caller's support issue."

        validation_response = await client.post(
            "/api/control/validate", headers=headers, json={"package": package}
        )
        validation = await validation_response.json()
        assert validation_response.status == 200
        assert validation["validation"]["valid"] is True

        staged_response = await client.post(
            "/api/control/stage",
            headers=headers,
            json={
                "package": package,
                "reason": "Production support package",
                "created_by": "codex-control-agent",
            },
        )
        staged = await staged_response.json()
        assert staged_response.status == 201
        deployment_id = staged["deployment"]["deployment_id"]
        assert staged["deployment"]["state"] == "staged"

        activated_response = await client.post(
            "/api/control/activate",
            headers=headers,
            json={"deployment_id": deployment_id},
        )
        activated = await activated_response.json()
        assert activated_response.status == 200, activated
        assert activated["deployment"]["state"] == "active"
        assert server.system_prompt == "Prioritize the caller's support issue."
        assert server.control_plane_store.active().deployment_id == deployment_id
        assert (tmp_path / "studio.json").is_file()
        assert (tmp_path / "tasks" / f"{server.task_id}.yaml").is_file()

        deployments = await client.get("/api/control/deployments", headers=headers)
        deployment_data = await deployments.json()
        assert deployment_data["deployments"][0]["deployment_id"] == deployment_id


@pytest.mark.asyncio
async def test_stale_package_and_in_call_activation_fail_closed(tmp_path: Path) -> None:
    server = _server(tmp_path)
    headers = {"Authorization": f"Bearer {server._control_token}"}
    async with TestClient(TestServer(server.app)) as client:
        package = (await (await client.get("/api/control/package", headers=headers)).json())[
            "package"
        ]
        package["package_id"] = "stale_test"
        staged = await (
            await client.post(
                "/api/control/stage",
                headers=headers,
                json={
                    "package": package,
                    "reason": "Stale write regression",
                    "created_by": "hermes-control-agent",
                },
            )
        ).json()
        server.system_prompt = "Changed after staging"
        response = await client.post(
            "/api/control/activate",
            headers=headers,
            json={"deployment_id": staged["deployment"]["deployment_id"]},
        )
        assert response.status == 409
        assert "changed after staging" in (await response.json())["message"]


@pytest.mark.asyncio
async def test_control_events_are_bounded_cursor_readable_and_redacted(tmp_path: Path) -> None:
    server = _server(tmp_path)
    headers = {"Authorization": f"Bearer {server._control_token}"}
    await server.broadcast(
        {"type": "transcript", "role": "user", "text": "Hello", "caller_id": "+33123456789"}
    )
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get(
            "/api/control/events?after=0&limit=10", headers=headers
        )
        payload = await response.json()
    assert response.status == 200
    assert payload["events"][0]["sequence"] == 1
    assert payload["events"][0]["caller_id"].startswith("sha256:")
    assert "+33123456789" not in json.dumps(payload)
