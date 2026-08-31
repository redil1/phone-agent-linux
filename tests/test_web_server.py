"""Unit tests for PhoneAgent Studio Web Server."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from phone_agent_gateway.ai_bridge.memory.memory_manager import LayeredMemoryManager
from phone_agent_gateway.ai_bridge.personality.persona_compiler import PersonaCompiler
from phone_agent_gateway.ai_bridge.production_security import AuditLedger
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer


def test_get_index_serves_html() -> None:
    async def _test() -> None:
        config = ProviderConfig(
            stt_provider="antigravity_live",
            llm_provider="antigravity_gemini",
            tts_provider="edge_tts",
            tts_voice_id="en-US-AndrewMultilingualNeural",
            stt_language="en-US",
        )
        server = PhoneAgentWebServer(config=config)
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.get("/")
            assert resp.status == 200
            text = await resp.text()
            assert "Adam AI Studio" in text or "PhoneAgent" in text
            assert 'id="edge-tts-voice"' in text
            assert 'id="google-tts-scene"' in text
            assert 'id="google-tts-sample-context"' in text
            assert 'id="google-tts-model"' in text
            assert 'id="chatgpt-realtime-transport"' in text
            assert 'id="chatgpt-realtime-reasoning"' in text
            assert 'id="tab-identity"' in text
            assert 'id="tab-tools"' in text
            assert 'id="tab-business"' in text
            assert "CRM &amp; ERP" in text
            assert "/api/frappe/test" in text
            assert 'id="frappe-campaign-enabled"' in text
            assert 'id="openwa-card"' in text
            assert text.index('id="tab-tools"') < text.index('id="openwa-card"')
            assert text.index('id="openwa-card"') < text.index('id="tab-pipeline"')
            assert "/api/openwa/provision" in text
            assert "test-status" in text
            assert "Testing…" in text
            assert 'id="identity-workflow-track"' in text
            assert "/api/identity/revisions/evaluate" in text
            assert "gemini-2.5-flash-preview-tts" in text
            assert "/api/tts/edge-voices" in text

    asyncio.run(_test())


def test_get_status_returns_idle() -> None:
    async def _test() -> None:
        config = ProviderConfig(
            stt_provider="antigravity_live",
            llm_provider="antigravity_gemini",
            tts_provider="edge_tts",
            tts_voice_id="en-US-AndrewMultilingualNeural",
            stt_language="en-US",
        )
        server = PhoneAgentWebServer(config=config)
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.get("/api/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["call_state"] == "IDLE"

    asyncio.run(_test())


def test_studio_rejects_remote_binding_dns_rebinding_and_cross_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHONE_AGENT_ALLOW_EXTERNAL", "0")
    with pytest.raises(ValueError, match="loopback"):
        PhoneAgentWebServer(host="192.168.1.50")

    async def _test() -> None:
        server = _studio()
        async with TestClient(TestServer(server.app)) as client:
            rebound = await client.get("/api/status", headers={"Host": "attacker.example"})
            assert rebound.status == 421
            cross_origin = await client.post(
                "/api/call/hangup", headers={"Origin": "https://attacker.example"}
            )
            assert cross_origin.status == 403
            normal = await client.get("/api/status")
            assert normal.headers["X-Frame-Options"] == "DENY"
            assert normal.headers["Cache-Control"] == "no-store"

    asyncio.run(_test())


def test_get_and_post_config(tmp_path: Path) -> None:
    async def _test() -> None:
        config = ProviderConfig(
            stt_provider="antigravity_live",
            llm_provider="antigravity_gemini",
            tts_provider="edge_tts",
            tts_voice_id="en-US-AndrewMultilingualNeural",
            stt_language="en-US",
        )
        server = PhoneAgentWebServer(config=config, settings_path=tmp_path / "studio.json")
        async with TestClient(TestServer(server.app)) as client:
            # 1. Get initial config
            resp = await client.get("/api/config")
            assert resp.status == 200
            data = await resp.json()
            assert data["tts_voice_id"] == "en-US-AndrewMultilingualNeural"
            assert data["stt_language"] == "en-US"

            # 2. Update config
            update_payload = {
                "tts_provider": "google_genai",
                "tts_model": "gemini-2.5-flash-preview-tts",
                "tts_voice_id": "Aoede",
                "tts_aggregation": "sentence",
                "stt_language": "fr-FR",
                "speculative_pipeline_enabled": False,
                "conversational_reflex_enabled": False,
                "google_tts_scene": "A clear, quiet English-language sales call.",
                "google_tts_sample_context": "Adam continues naturally and calmly.",
            }
            post_resp = await client.post("/api/config", json=update_payload)
            assert post_resp.status == 200
            result = await post_resp.json()
            assert result["status"] == "ok"

            # 3. Verify updated config
            resp2 = await client.get("/api/config")
            data2 = await resp2.json()
            assert data2["tts_voice_id"] == "Aoede"
            assert data2["tts_provider"] == "google_genai"
            assert data2["tts_model"] == "gemini-2.5-flash-preview-tts"
            assert data2["stt_language"] == "fr-FR"
            assert data2["speculative_pipeline_enabled"] is False
            assert server._child_environment()["PHONE_AGENT_SPECULATIVE_PIPELINE"] == "false"
            assert data2["conversational_reflex_enabled"] is False
            assert server._child_environment()["PHONE_AGENT_CONVERSATIONAL_REFLEX"] == "false"
            assert data2["google_tts_scene"] == "A clear, quiet English-language sales call."
            assert data2["google_tts_sample_context"] == "Adam continues naturally and calmly."
            assert (
                server._child_environment()["PHONE_AGENT_GOOGLE_TTS_SCENE"]
                == "A clear, quiet English-language sales call."
            )
            assert (
                server._child_environment()["PHONE_AGENT_TTS_MODEL"]
                == "gemini-2.5-flash-preview-tts"
            )

        reloaded = PhoneAgentWebServer(settings_path=tmp_path / "studio.json")
        assert reloaded.config.google_tts_scene == "A clear, quiet English-language sales call."
        assert reloaded.config.google_tts_sample_context == "Adam continues naturally and calmly."

    asyncio.run(_test())


def test_websocket_sync_on_connect() -> None:
    async def _test() -> None:
        config = ProviderConfig(
            stt_provider="antigravity_live",
            llm_provider="antigravity_gemini",
            tts_provider="edge_tts",
            tts_voice_id="en-US-AndrewMultilingualNeural",
            stt_language="en-US",
        )
        server = PhoneAgentWebServer(config=config)
        async with TestClient(TestServer(server.app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await ws.receive_str()
            data = json.loads(msg)
            assert data["type"] == "status_sync"
            assert data["call_state"] == "IDLE"
            await ws.close()

    asyncio.run(_test())


def test_persona_task_and_child_configuration_apply_end_to_end(tmp_path: Path) -> None:
    async def _test() -> None:
        persona_path = tmp_path / "persona.yaml"
        persona_path.write_text(
            "identity:\n  name: Original\n  role: Caller assistant\n",
            encoding="utf-8",
        )
        server = PhoneAgentWebServer(
            persona_compiler=PersonaCompiler(persona_path=persona_path),
            memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
            settings_path=tmp_path / "studio.json",
        )
        async with TestClient(TestServer(server.app)) as client:
            persona_response = await client.post(
                "/api/persona",
                json={
                    "identity": {
                        "name": "Reception Agent",
                        "role": "Appointment assistant",
                        "mission": "Understand the caller and collect the needed details",
                    }
                },
            )
            assert persona_response.status == 200
            assert (
                PersonaCompiler(persona_path=persona_path).persona_data["identity"]["name"]
                == "Reception Agent"
            )

            config_response = await client.post(
                "/api/config",
                json={
                    "task_id": "booking_appointment",
                    "system_prompt": "Explain the reason for the call and never rush the caller.",
                },
            )
            assert config_response.status == 200
            child_env = server._child_environment()
            assert child_env["PHONE_AGENT_TASK_ID"] == "booking_appointment"
            assert "never rush" in child_env["PHONE_AGENT_SYSTEM_PROMPT"]
            assert child_env["PHONE_AGENT_PERSONA_PATH"] == str(persona_path)
            assert child_env["PHONE_AGENT_EVENT_STREAM"] == "true"
            assert (tmp_path / "studio.json").exists()

            live_memory = LayeredMemoryManager(storage_path=tmp_path / "memory.json")
            live_memory.update_preferences("+212600000000", {"preferred_language": "fr-FR"})
            memory_response = await client.get("/api/memory")
            memory_data = await memory_response.json()
            assert memory_data["callers"][0]["phone_number"] == "+212600000000"

            invalid = await client.post("/api/config", json={"task_id": "missing"})
            assert invalid.status == 400

        reloaded = PhoneAgentWebServer(settings_path=tmp_path / "studio.json")
        assert reloaded.task_id == "booking_appointment"
        assert "never rush" in reloaded.system_prompt

    asyncio.run(_test())


def test_identity_revision_evaluation_approval_and_activation_api(tmp_path: Path) -> None:
    async def _test() -> None:
        persona_path = tmp_path / "persona.yaml"
        persona_path.write_text(
            "identity:\n  name: Adam\n  role: AI phone representative\n"
            "  mission: Help callers make accurate decisions in natural conversations.\n",
            encoding="utf-8",
        )
        server = PhoneAgentWebServer(
            persona_compiler=PersonaCompiler(persona_path=persona_path),
            memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
            settings_path=tmp_path / "studio.json",
        )
        async with TestClient(TestServer(server.app)) as client:
            initial_response = await client.get("/api/identity")
            initial = await initial_response.json()
            assert initial_response.status == 200
            assert initial["evaluation"]["passed"] is True
            assert initial["active"]["version"] == 1
            profile = initial["active"]
            profile["core"]["mission"] = (
                "Help callers make truthful decisions with concise, warm and safe conversations."
            )
            created_response = await client.post(
                "/api/identity/revisions",
                json={"profile": profile, "reason": "Improve the mission"},
            )
            created = await created_response.json()
            assert created_response.status == 201
            revision_id = created["revision"]["revision_id"]

            evaluated_response = await client.post(
                "/api/identity/revisions/evaluate", json={"revision_id": revision_id}
            )
            evaluated = await evaluated_response.json()
            assert evaluated["revision"]["evaluation"]["passed"] is True

            approved_response = await client.post(
                "/api/identity/revisions/approve", json={"revision_id": revision_id}
            )
            assert approved_response.status == 200
            activated_response = await client.post(
                "/api/identity/revisions/activate", json={"revision_id": revision_id}
            )
            activated = await activated_response.json()
            assert activated_response.status == 200
            assert activated["active"]["version"] == 2

            final = await (await client.get("/api/identity")).json()
            assert final["active"]["core"]["mission"] == profile["core"]["mission"]
            immutable = next(
                item for item in final["memory_blocks"] if item["block_id"] == "core_self"
            )
            immutable["content"] = "tampered"
            refused = await client.post("/api/identity/memory-blocks", json={"block": immutable})
            assert refused.status == 400

            authored_response = await client.post(
                "/api/identity/skills",
                json={
                    "name": "order-support",
                    "description": (
                        "Handle order questions using verified order tools and concise answers."
                    ),
                    "version": "1.0.0",
                    "instructions": (
                        "Load the verified order before speaking. Never invent a delivery date."
                    ),
                    "allowed_tools": ["lookup_order"],
                    "mcp_tools": [],
                    "task_ids": ["customer_support"],
                    "languages": ["en", "fr"],
                    "priority": 50,
                },
            )
            authored = await authored_response.json()
            assert authored_response.status == 201
            assert authored["skill"]["trusted"] is False
            trusted_response = await client.post(
                "/api/identity/skills/trust",
                json={
                    "name": "order-support",
                    "digest": authored["skill"]["digest"],
                },
            )
            assert trusted_response.status == 200
            refreshed = await (await client.get("/api/identity")).json()
            order_skill = next(
                item for item in refreshed["skills"] if item["name"] == "order-support"
            )
            assert order_skill["trusted"] is True

    asyncio.run(_test())


def test_admin_can_approve_after_contract_check_without_live_evaluation(tmp_path: Path) -> None:
    async def _test() -> None:
        persona_path = tmp_path / "persona.yaml"
        persona_path.write_text(
            "identity:\n  name: Adam\n  role: AI phone representative\n"
            "  mission: Help callers make accurate decisions in natural conversations.\n",
            encoding="utf-8",
        )
        server = PhoneAgentWebServer(
            config=_studio().config,
            persona_compiler=PersonaCompiler(persona_path=persona_path),
            memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
            settings_path=tmp_path / "studio.json",
        )
        async with TestClient(TestServer(server.app)) as client:
            initial = await (await client.get("/api/identity")).json()
            candidate = initial["active"]
            candidate["core"]["hard_boundaries"] = []
            candidate["core"]["forbidden_behaviors"] = []
            created = await (
                await client.post(
                    "/api/identity/revisions",
                    json={"profile": candidate, "reason": "Administrator-defined identity"},
                )
            ).json()
            revision_id = created["revision"]["revision_id"]
            checked = await client.post(
                "/api/identity/revisions/evaluate", json={"revision_id": revision_id}
            )
            checked_payload = await checked.json()
            assert checked.status == 200
            assert checked_payload["revision"]["evaluation"]["passed"] is True
            removed_live_endpoint = await client.post(
                "/api/identity/revisions/evaluate-live", json={"revision_id": revision_id}
            )
            assert removed_live_endpoint.status == 404
            approved = await client.post(
                "/api/identity/revisions/approve", json={"revision_id": revision_id}
            )
            assert approved.status == 200
            activated = await client.post(
                "/api/identity/revisions/activate", json={"revision_id": revision_id}
            )
            assert activated.status == 200
            ready = await (await client.get("/api/identity")).json()
            assert ready["production_status"]["ready"] is True
            assert ready["production_status"]["live_required"] is False
            historical = ready["history"][0]
            restored = await client.post(
                "/api/identity/history/restore",
                json={"history_file": historical["file"]},
            )
            restored_payload = await restored.json()
            assert restored.status == 200
            assert restored_payload["active"]["version"] == historical["version"]
            assert restored_payload["profile_hash"] == historical["profile_hash"]
            after_restore = await (await client.get("/api/identity")).json()
            assert after_restore["active"]["version"] == historical["version"]
            assert after_restore["profile_hash"] == historical["profile_hash"]
            assert all(
                item["state"] not in {"draft", "evaluated", "approved"}
                for item in after_restore["revisions"]
            )

    asyncio.run(_test())


def test_child_call_error_is_forwarded_without_duplicate_fallback() -> None:
    async def _test() -> None:
        server = _studio()
        received: list[dict[str, object]] = []

        async def capture(event: dict[str, object]) -> None:
            received.append(event)

        server.broadcast = capture  # type: ignore[method-assign]
        event = {
            "type": "call_error",
            "message": "another PhoneAgent voice host is already running",
        }
        await server._handle_child_line(
            "PHONE_AGENT_EVENT " + json.dumps(event, separators=(",", ":"))
        )

        assert server._child_reported_error is True
        assert received == [event]

    asyncio.run(_test())


def test_authoritative_stt_diagnostics_are_retained(caplog) -> None:
    async def _test() -> None:
        server = _studio()
        with caplog.at_level(logging.INFO):
            await server._handle_child_line(
                "AntigravityLiveSTT Suppressed late STT revision without new speech"
            )

        assert "Voice STT diagnostic" in caplog.text
        assert "Suppressed late STT revision" in caplog.text

    asyncio.run(_test())


def test_tts_provider_recovery_diagnostics_are_retained(caplog) -> None:
    async def _test() -> None:
        server = _studio()
        with caplog.at_level(logging.WARNING):
            await server._handle_child_line(
                "Using Gemini TTS model fallback primary=gemini-3.1 fallback=gemini-2.5"
            )

        assert "Voice TTS diagnostic" in caplog.text
        assert "Using Gemini TTS model fallback" in caplog.text

    asyncio.run(_test())


def test_edge_live_retry_diagnostics_are_retained(caplog) -> None:
    async def _test() -> None:
        server = _studio()
        with caplog.at_level(logging.WARNING):
            await server._handle_child_line(
                "Edge TTS live attempt failed attempt=1/3 chars=80 error=network; retrying"
            )

        assert "Voice TTS diagnostic" in caplog.text
        assert "Edge TTS live attempt failed" in caplog.text

    asyncio.run(_test())


def test_gateway_media_recovery_diagnostics_are_retained(caplog) -> None:
    async def _test() -> None:
        server = _studio()
        with caplog.at_level(logging.WARNING):
            await server._handle_child_line(
                "recovered authenticated phone link in place call_id=one epoch=two"
            )

        assert "Voice gateway diagnostic" in caplog.text
        assert "recovered authenticated phone link in place" in caplog.text

    asyncio.run(_test())


def test_edge_voice_endpoint_returns_live_catalog(monkeypatch) -> None:
    async def _test() -> None:
        async def fake_catalog() -> list[dict[str, object]]:
            return [
                {
                    "short_name": "en-US-AndrewMultilingualNeural",
                    "locale": "en-US",
                    "gender": "Male",
                    "display_name": "Andrew — Multilingual",
                    "friendly_name": "Microsoft Andrew",
                    "multilingual": True,
                    "status": "GA",
                }
            ]

        monkeypatch.setattr(
            "phone_agent_gateway.ai_bridge.web_server.fetch_edge_voice_catalog",
            fake_catalog,
        )
        server = _studio()
        async with TestClient(TestServer(server.app)) as client:
            response = await client.get("/api/tts/edge-voices")
            data = await response.json()

        assert response.status == 200
        assert data["source"] == "live"
        assert data["voices"][0]["short_name"] == "en-US-AndrewMultilingualNeural"

    asyncio.run(_test())


def test_edge_voice_endpoint_has_network_failure_fallback(monkeypatch) -> None:
    async def _test() -> None:
        async def unavailable() -> list[dict[str, object]]:
            raise ConnectionError("offline")

        monkeypatch.setattr(
            "phone_agent_gateway.ai_bridge.web_server.fetch_edge_voice_catalog",
            unavailable,
        )
        server = _studio()
        async with TestClient(TestServer(server.app)) as client:
            response = await client.get("/api/tts/edge-voices")
            data = await response.json()

        assert response.status == 200
        assert data["source"] == "fallback"
        assert len(data["voices"]) == 30

    asyncio.run(_test())


def _studio() -> PhoneAgentWebServer:
    return PhoneAgentWebServer(
        config=ProviderConfig(
            stt_provider="parakeet_local",
            llm_provider="ollama",
            tts_provider="edge_tts",
            tts_voice_id="en-US-AndrewMultilingualNeural",
            stt_language="en-US",
        )
    )


def test_stale_dial_task_does_not_block_the_next_call() -> None:
    """A task whose child already exited must never strand the Studio.

    Hang Up terminated the child but left the task pending, so every later dial
    answered 409 and only a Studio restart recovered it.
    """

    async def _test() -> None:
        server = _studio()
        pending: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        server._dial_task = asyncio.create_task(asyncio.wait_for(pending, None))
        await asyncio.sleep(0)
        try:
            server._active_process = None
            assert server._dial_in_progress() is False

            server._active_process = SimpleNamespace(returncode=0)
            assert server._dial_in_progress() is False

            server._active_process = SimpleNamespace(returncode=None)
            assert server._dial_in_progress() is True
        finally:
            server._active_process = None
            await server._cancel_dial_task()
        assert server._dial_task is None

    asyncio.run(_test())


def test_hangup_cancels_a_wedged_dial_task() -> None:
    async def _test() -> None:
        server = _studio()
        pending: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        server._dial_task = asyncio.create_task(asyncio.wait_for(pending, None))
        await asyncio.sleep(0)

        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post("/api/call/hangup")
            assert resp.status == 200

        assert server._dial_task is None
        assert server.call_state == "IDLE"

    asyncio.run(_test())


def test_hangup_terminates_the_owned_voice_process_and_returns_idle() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.terminated = False
            self.killed = False

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def _test() -> None:
        server = _studio()
        process = FakeProcess()
        server._active_process = process  # type: ignore[assignment]
        server.call_state = "ACTIVE"
        pending: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        server._dial_task = asyncio.create_task(asyncio.wait_for(pending, None))

        async with TestClient(TestServer(server.app)) as client:
            response = await client.post("/api/call/hangup")
            payload = await response.json()

        assert response.status == 200
        assert payload == {"status": "ok", "message": "Call ended."}
        assert process.terminated is True
        assert process.killed is False
        assert server._dial_task is None
        assert server.call_state == "IDLE"

    asyncio.run(_test())


def test_dial_fails_fast_when_android_gateway_is_unavailable(tmp_path: Path) -> None:
    async def _test() -> None:
        server = _studio()
        server.audit_ledger = AuditLedger(tmp_path / "audit.jsonl")

        async def unavailable() -> str:
            return "Android phone is not connected to ADB."

        server._gateway_preflight = unavailable  # type: ignore[method-assign]
        async with TestClient(TestServer(server.app)) as client:
            response = await client.post(
                "/api/call/dial",
                json={"phone_number": "0660000000", "operator_approved": True},
            )
            payload = await response.json()

        assert response.status == 503
        assert payload["status"] == "error"
        assert "not connected" in payload["message"]
        assert server.call_state == "IDLE"
        assert server._dial_task is None

    asyncio.run(_test())


def test_dial_requires_explicit_operator_approval_and_redacts_audit(tmp_path: Path) -> None:
    async def _test() -> None:
        audit_path = tmp_path / "audit.jsonl"
        server = _studio()
        server.audit_ledger = AuditLedger(audit_path)
        async with TestClient(TestServer(server.app)) as client:
            response = await client.post("/api/call/dial", json={"phone_number": "0660000000"})
            payload = await response.json()

        assert response.status == 403
        assert "approval" in payload["message"]
        audit = audit_path.read_text()
        assert "0660000000" not in audit
        assert "last4:0000" in audit

    asyncio.run(_test())


def test_recording_consent_is_explicitly_forwarded_to_call_process() -> None:
    server = _studio()
    disabled = server._child_environment()
    enabled = server._child_environment(recording_consent=True)
    assert disabled["PHONE_AGENT_RECORDING_ENABLED"] == "false"
    assert disabled["PHONE_AGENT_RECORDING_CONSENT"] == "false"
    assert enabled["PHONE_AGENT_RECORDING_ENABLED"] == "true"
    assert enabled["PHONE_AGENT_RECORDING_CONSENT"] == "true"


def test_inbound_receptionist_environment_forces_gsm_auto_answer_without_recording() -> None:
    server = _studio()
    environment = server._child_environment(
        auto_answer=True,
        call_channel="gsm",
        recording_consent=False,
    )

    assert environment["PHONE_AGENT_AUTO_ANSWER"] == "true"
    assert environment["PHONE_AGENT_CALL_CHANNEL"] == "gsm"
    assert environment["PHONE_AGENT_RECORDING_ENABLED"] == "false"
    assert environment["PHONE_AGENT_RECORDING_CONSENT"] == "false"


def test_auto_answer_setting_starts_and_stops_receptionist(tmp_path: Path) -> None:
    async def _test() -> None:
        server = _studio()
        server.settings_path = tmp_path / "studio.json"
        actions: list[str] = []

        async def start() -> None:
            actions.append("start")
            server.receptionist_state = "listening"

        async def stop() -> None:
            actions.append("stop")
            server.receptionist_state = "disabled"

        server._start_inbound_monitor = start  # type: ignore[method-assign]
        server._stop_inbound_monitor = stop  # type: ignore[method-assign]
        async with TestClient(TestServer(server.app)) as client:
            actions.clear()  # Ignore the normal app-startup hook while disabled.
            enabled = await client.post("/api/config", json={"auto_answer_enabled": True})
            enabled_payload = await enabled.json()
            assert enabled.status == 200
            assert enabled_payload["config"]["auto_answer_enabled"] is True
            # Both directions restart the host rather than only starting or only
            # stopping it. The child's auto_answer is baked into its environment
            # at spawn, so answering cannot be turned on or off in place; and
            # turning it off must not discard a warm host, because the next
            # outbound dial reuses that host to skip the ~20 s model load.
            assert actions == ["stop", "start"]
            disabled = await client.post("/api/config", json={"auto_answer_enabled": False})
            assert disabled.status == 200
            assert actions == ["stop", "start", "stop", "start"]

        saved = json.loads((tmp_path / "studio.json").read_text())
        assert saved["auto_answer_enabled"] is False

    asyncio.run(_test())


def test_inbound_supervisor_reports_listening_and_uses_no_dial_argument(monkeypatch) -> None:
    class FakeStdout:
        def __init__(self) -> None:
            self.lines = iter(
                [
                    b"gateway control ready call_id=test\n",
                    b"cellular state=RINGING\n",
                    b"cellular state=ACTIVE\n",
                    b"cellular state=IDLE\n",
                ]
            )

        async def readline(self) -> bytes:
            return next(self.lines, b"")

    class FakeProcess:
        def __init__(self, server: PhoneAgentWebServer) -> None:
            self.server = server
            self.stdout = FakeStdout()
            self.returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            # Ends the supervisor. Turning auto-answer off used to do this, but
            # the warm host now outlives that setting -- only shutdown stops the
            # loop, or an exited host would never be replaced.
            self.server._shutting_down = True
            return 0

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

    async def _test() -> None:
        server = _studio()
        server.auto_answer_enabled = True
        spawned: list[tuple[tuple[object, ...], dict[str, object]]] = []
        broadcasts: list[dict[str, object]] = []

        async def create_process(*args, **kwargs):
            spawned.append((args, kwargs))
            return FakeProcess(server)

        async def broadcast(event: dict[str, object]) -> None:
            broadcasts.append(event)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
        server.broadcast = broadcast  # type: ignore[method-assign]
        await server._inbound_monitor_supervisor()

        args, kwargs = spawned[0]
        assert args[-1] == "phone_agent_gateway.ai_bridge.phone_voice_agent"
        assert "--dial" not in args
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["PHONE_AGENT_AUTO_ANSWER"] == "true"
        assert environment["PHONE_AGENT_CALL_CHANNEL"] == "gsm"
        states = [
            event["state"] for event in broadcasts if event.get("type") == "receptionist_status"
        ]
        assert states[:2] == ["starting", "listening"]
        assert server.call_state == "IDLE"

    asyncio.run(_test())


def test_child_failure_output_is_logged_not_discarded(caplog) -> None:
    """A crashed voice host used to match no marker and vanish from the log."""

    async def _test() -> None:
        server = _studio()
        await server._handle_child_line("Traceback (most recent call last):")
        await server._handle_child_line("ConfigurationError: unsupported tts provider")

    with caplog.at_level(logging.ERROR):
        asyncio.run(_test())
    assert any("Voice host failure" in record.message for record in caplog.records)


def test_task_contract_can_be_authored_and_deleted(tmp_path: Path) -> None:
    """Studio-authored tasks persist beside the persona, not in the package."""

    from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

    engine = TaskEngine(user_contracts_dir=tmp_path / "tasks")
    shipped = {contract["id"] for contract in engine.get_all_contracts()}
    assert "iptv_subscription_sales" in shipped

    saved = engine.save_contract(
        {
            "id": "renewal_outreach",
            "title": "Renewal Outreach",
            "objective": "Renew lapsed subscribers.",
            "opening_greeting": {"fr": "Bonjour, ici Adam."},
            "spoken_max_words": 30,
            "conversation_strategy": ["OPEN: greet and confirm identity."],
            "stop_conditions": ["caller_requests_no_more_calls"],
        }
    )
    assert saved["id"] == "renewal_outreach"
    assert engine.require_contract("renewal_outreach")["title"] == "Renewal Outreach"
    assert engine.is_user_authored("renewal_outreach") is True
    assert engine.is_user_authored("iptv_subscription_sales") is False

    engine.delete_contract("renewal_outreach")
    assert engine.get_contract("renewal_outreach") is None


def test_shipped_task_cannot_be_deleted(tmp_path: Path) -> None:
    from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

    engine = TaskEngine(user_contracts_dir=tmp_path / "tasks")
    with pytest.raises(ValueError, match="not a Studio-authored task"):
        engine.delete_contract("iptv_subscription_sales")


def test_authored_task_overrides_the_shipped_one(tmp_path: Path) -> None:
    """Editing a shipped task must replace it, not duplicate it."""

    from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

    engine = TaskEngine(user_contracts_dir=tmp_path / "tasks")
    engine.save_contract(
        {
            "id": "iptv_subscription_sales",
            "title": "My Edited IPTV Task",
            "objective": "Edited objective.",
        }
    )
    ids = [contract["id"] for contract in engine.get_all_contracts()]
    assert ids.count("iptv_subscription_sales") == 1
    assert engine.require_contract("iptv_subscription_sales")["title"] == "My Edited IPTV Task"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"id": "Bad Id", "title": "t", "objective": "o"}, "task id must be"),
        ({"id": "ok_id", "title": "", "objective": "o"}, "title is required"),
        ({"id": "ok_id", "title": "t", "objective": ""}, "objective is required"),
        ({"id": "ok_id", "title": "t", "objective": "o", "jailbreak": []}, "unsupported task"),
        (
            {"id": "ok_id", "title": "t", "objective": "o", "spoken_max_words": 9999},
            "must be between",
        ),
        (
            {"id": "ok_id", "title": "t", "objective": "o", "opening_greeting": {"de": "hi"}},
            "supports en and fr",
        ),
        (
            {"id": "ok_id", "title": "t", "objective": "o", "stop_conditions": "not a list"},
            "must be a list",
        ),
    ],
)
def test_invalid_task_contracts_are_refused(payload: dict, message: str) -> None:
    from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

    with pytest.raises(ValueError, match=message):
        TaskEngine.validate_contract(payload)


def test_oversized_task_contract_is_refused() -> None:
    """An imported task must not be able to bloat the system instruction."""

    from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

    bulk = ["x" * 390 for _ in range(40)]
    with pytest.raises(ValueError, match="too large"):
        TaskEngine.validate_contract(
            {
                "id": "big_task",
                "title": "t",
                "objective": "o",
                "conversation_strategy": bulk,
                "success_criteria": bulk,
                "natural_conversation_rules": bulk,
            }
        )


def test_the_studio_page_never_renders_an_undefined_provider() -> None:
    """The page is served from disk while the server stays in memory.

    A Studio left running across an upgrade answers with the older payload
    shape, and the menu rendered "undefined" for every provider. The page must
    accept either shape and fall back to the provider's own name.
    """

    from phone_agent_gateway.ai_bridge.web_server import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "function normaliseProviders" in page
    # The raw payload must never be assigned straight through again.
    assert "data.providers || {}" not in page
    assert "productProviders = normaliseProviders(data.providers)" in page


def test_the_task_editor_preserves_fields_it_does_not_show() -> None:
    """Saving an edited task must not delete what product research verified.

    The editor renders a fixed set of fields and rebuilt the contract from them
    alone, so opening an imported task and pressing Save silently destroyed its
    knowledge block, spoken examples and objection playbook.
    """

    from phone_agent_gateway.ai_bridge.web_server import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "taskFieldsNotInEditor" in page
    assert "...taskFieldsNotInEditor," in page, "collectTaskFromForm must spread them back in"
    # The set of managed fields has to be derived, not hand-listed, or a new
    # contract field silently becomes destroyable again.
    assert "...TASK_LISTS.map(([, key]) => key)," in page
    assert page.index("const TASK_LISTS") < page.index("EDITOR_MANAGED_FIELDS")


def test_core_self_is_operator_editable_through_a_safe_revision() -> None:
    """The operator must not be locked out of their agent's core identity.

    Core Self remains derived from the evaluated constitution, but the Studio
    must expose an obvious route to edit it through the protected revision
    workflow instead of presenting it as permanently immutable.
    """

    from phone_agent_gateway.ai_bridge.web_server import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "Core Identity — operator-editable by revision" in page
    assert "function editCoreIdentity()" in page
    assert ">Edit Core Identity</button>" in page
    assert "Stage → Evaluate → Approve → Activate" in page


def test_studio_displays_automatic_inbound_or_outbound_call_context() -> None:
    from phone_agent_gateway.ai_bridge.web_server import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="call-context-pill"' in page
    assert 'id="call-context-pill" style="display:none;"' not in page
    assert "msg.type === 'call_context'" in page
    assert "Outbound · cold prospecting" in page
    assert "Inbound · caller intent" in page
    assert ".status-group #server-status-pill" in page
    assert 'id="auto-answer-enabled"' in page
    assert "function setAutoAnswer()" in page
    assert "AI answers incoming GSM calls" in page
    assert "msg.type === 'receptionist_status'" in page


def test_archived_identity_button_restores_and_activates_in_one_action() -> None:
    from phone_agent_gateway.ai_bridge.web_server import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "Archived versions are older identities that were previously active." in page
    assert "Restore &amp; Activate v" in page
    assert "function restoreIdentityHistory" in page
    assert "/api/identity/history/restore" in page
    assert "stageIdentityRollback" not in page
    assert "if (currentIdentityRevision)" in page
    assert "item.revision_id === currentIdentityRevision.revision_id" in page


def test_command_channel_is_only_declared_for_a_resident_host() -> None:
    server = _studio()

    assert "PHONE_AGENT_COMMAND_STDIN" not in server._child_environment()
    assert server._child_environment(command_stdin=True)["PHONE_AGENT_COMMAND_STDIN"] == "true"


def test_resident_host_serves_a_dial_without_spawning_a_process() -> None:
    """A warm host already holds the models, so dialling must not reload them."""

    async def _test() -> None:
        server = _studio()
        written: list[bytes] = []

        class _Writer:
            def write(self, payload: bytes) -> None:
                written.append(payload)

            async def drain(self) -> None:
                return None

        server._receptionist_process = SimpleNamespace(returncode=None, stdin=_Writer())

        async def finish_shortly() -> None:
            await asyncio.sleep(0)
            server._note_warm_call_state("DIALING")
            server._note_warm_call_state("ACTIVE")
            server._note_warm_call_state("IDLE")

        asyncio.get_running_loop().create_task(finish_shortly())
        await server._execute_dial("+212600000000", recording_consent=False)

        assert json.loads(written[0].decode()) == {
            "command": "dial",
            "number": "+212600000000",
        }
        # No child was spawned, and the call still closed out to idle.
        assert server._active_process is None
        assert server.call_state == "IDLE"

    asyncio.run(_test())


def test_a_consented_recording_never_reuses_the_resident_host() -> None:
    """Consent is fixed in a host's environment when it starts, so it needs its own."""

    server = _studio()
    server._receptionist_process = SimpleNamespace(returncode=None, stdin=object())

    assert server._resident_host_stdin() is not None
    # The dial path consults this only when consent is absent.
    assert server._child_environment(recording_consent=True)["PHONE_AGENT_RECORDING_CONSENT"] == (
        "true"
    )


def test_an_exited_resident_host_is_never_dialled() -> None:
    server = _studio()
    server._receptionist_process = SimpleNamespace(returncode=1, stdin=object())
    assert server._resident_host_stdin() is None

    server._receptionist_process = None
    assert server._resident_host_stdin() is None


def test_warm_call_reaches_finished_only_after_it_went_live() -> None:
    server = _studio()

    # An idle report before any call must not release a waiter.
    server._note_warm_call_state("IDLE")
    assert not server._warm_call_finished.is_set()

    server._note_warm_call_state("ACTIVE")
    assert server._warm_call_active
    assert not server._warm_call_finished.is_set()

    server._note_warm_call_state("IDLE")
    assert server._warm_call_finished.is_set()
    assert not server._warm_call_active


def test_warm_host_reports_warm_once_models_load_even_with_no_gateway(monkeypatch) -> None:
    """The warm host's job is holding the speech models, not reaching the phone.

    Its whole purpose is that the next dial skips the ~20 s SenseVoice/Kokoro
    load, and that is achieved the moment the child reports "speech providers
    ready". Waiting for "gateway control ready" meant that with the handset
    offline the host sat at "starting" indefinitely while its models were in
    fact loaded, so the Studio reported the opposite of the truth.
    """

    class FakeStdout:
        def __init__(self) -> None:
            # No "gateway control ready": the phone is not reachable.
            self.lines = iter([b"speech providers ready stt=sensevoice tts=kokoro\n"])

        async def readline(self) -> bytes:
            return next(self.lines, b"")

    class FakeProcess:
        def __init__(self, server: PhoneAgentWebServer) -> None:
            self.server = server
            self.stdout = FakeStdout()
            self.returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            self.server._shutting_down = True
            return 0

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

    async def _test() -> None:
        server = _studio()
        server.auto_answer_enabled = False

        async def create_process(*args, **kwargs):
            return FakeProcess(server)

        async def broadcast(event: dict[str, object]) -> None:
            return None

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
        monkeypatch.setattr(server, "broadcast", broadcast)
        await server._inbound_monitor_supervisor()
        assert server.receptionist_state == "warm"

    asyncio.run(_test())


def test_a_warm_host_that_exits_is_replaced_even_when_auto_answer_is_off(monkeypatch) -> None:
    """A host that dies must be respawned, or every later dial pays the cold start.

    The supervisor used to leave its loop whenever auto-answer was off. That was
    correct while the only warm host *was* the inbound receptionist, but once the
    host became unconditional it meant a single exit silently disabled warm
    dialing for the rest of the process's life.
    """

    spawns: list[int] = []

    class FakeStdout:
        async def readline(self) -> bytes:
            return b""

    class FakeProcess:
        def __init__(self, server: PhoneAgentWebServer) -> None:
            self.server = server
            self.stdout = FakeStdout()
            self.returncode = None

        async def wait(self) -> int:
            self.returncode = 1
            # Stop after the host has been replaced once.
            if len(spawns) >= 2:
                self.server._shutting_down = True
            return 1

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = -9

    async def _test() -> None:
        server = _studio()
        server.auto_answer_enabled = False

        async def create_process(*args, **kwargs):
            spawns.append(1)
            return FakeProcess(server)

        async def broadcast(event: dict[str, object]) -> None:
            return None

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
        monkeypatch.setattr(asyncio, "sleep", no_sleep)
        monkeypatch.setattr(server, "broadcast", broadcast)
        await server._inbound_monitor_supervisor()
        assert len(spawns) >= 2, "the exited warm host was never replaced"

    asyncio.run(_test())
