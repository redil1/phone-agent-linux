from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from phone_agent_gateway.ai_bridge.openwa_integration import (
    OpenWAConfig,
    OpenWAConfigStore,
    OpenWAError,
    OpenWAEventBridge,
    OpenWAToolRuntime,
)
from phone_agent_gateway.ai_bridge.production_security import AuditLedger
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
from phone_agent_gateway.ai_bridge.tasks.tool_catalog import execute_tool
from phone_agent_gateway.ai_bridge.tool_control import MASKED_SECRET, ToolApprovalQueue
from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer

SESSION_ID = "0a941dac-a965-45e7-b318-74ae8be134f0"
CALLER = "+212600123456"
CHAT_ID = "212600123456@c.us"


def config(base_url: str, *enabled_tools: str) -> OpenWAConfig:
    policies = []
    for policy in OpenWAConfig().tools:
        policies.append(
            policy.model_copy(
                update={
                    "enabled": policy.name in enabled_tools,
                    "approval_mode": "never",
                    "task_ids": ["iptv"],
                }
            )
        )
    return OpenWAConfig(
        enabled=True,
        base_url=base_url,
        api_key="owa_phoneagent_key",
        session_id=SESSION_ID,
        live_events_enabled=False,
        tools=policies,
        allowed_media_hosts=["cdn.example.com"],
    )


def test_config_store_masks_and_preserves_api_key(tmp_path: Path) -> None:
    store = OpenWAConfigStore(tmp_path / "openwa.json")
    saved = store.save(
        OpenWAConfig(
            base_url="http://127.0.0.1:2785",
            api_key="private-key",
            session_id=SESSION_ID,
        ).model_dump(mode="json")
    )
    assert saved.revision == 1
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    public = store.public_state()
    assert public["api_key"] == MASKED_SECRET
    assert "private-key" not in json.dumps(public)
    again = store.save(public)
    assert again.api_key == "private-key"


def test_remote_plain_http_and_incomplete_activation_fail_closed() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        OpenWAConfig(base_url="http://example.com:2785")
    with pytest.raises(ValueError, match="requires an API key"):
        OpenWAConfig(enabled=True, session_id=SESSION_ID)


def test_new_openwa_tools_default_to_autonomous_current_caller_execution() -> None:
    assert {tool.approval_mode for tool in OpenWAConfig().tools} == {"never"}


@pytest.mark.asyncio
async def test_current_customer_tools_resolve_recipient_and_never_accept_a_model_jid() -> None:
    calls: list[tuple[str, dict]] = []

    async def check(request: web.Request) -> web.Response:
        assert request.match_info["number"] == "212600123456"
        return web.json_response(
            {"number": "212600123456", "exists": True, "whatsappId": CHAT_ID}
        )

    async def send_text(request: web.Request) -> web.Response:
        payload = await request.json()
        calls.append(("send-text", payload))
        return web.json_response(
            {"messageId": "msg-1", "timestamp": 1_700_000_000}, status=201
        )

    async def reply(request: web.Request) -> web.Response:
        payload = await request.json()
        calls.append(("reply", payload))
        return web.json_response({"messageId": "reply-1"}, status=201)

    async def history(request: web.Request) -> web.Response:
        assert request.query["chatId"] == CHAT_ID
        return web.json_response(
            {
                "messages": [
                    {"id": "incoming-1", "from": CHAT_ID, "body": "Hello", "type": "text"}
                ],
                "total": 1,
            }
        )

    app = web.Application()
    app.router.add_get("/api/sessions/{session}/contacts/check/{number}", check)
    app.router.add_post("/api/sessions/{session}/messages/send-text", send_text)
    app.router.add_post("/api/sessions/{session}/messages/reply", reply)
    app.router.add_get("/api/sessions/{session}/messages", history)
    async with TestServer(app) as upstream:
        runtime = OpenWAToolRuntime(
            config(
                str(upstream.make_url("/"))[:-1],
                "whatsapp_send_text_current_customer",
                "whatsapp_read_current_customer_chat",
                "whatsapp_reply_current_customer",
            ),
            caller_id=CALLER,
            task_id="iptv",
            call_id="call-1",
        )
        try:
            catalog = await runtime.start()
            sent = json.loads(
                await execute_tool(
                    catalog,
                    "whatsapp_send_text_current_customer",
                    json.dumps({"text": "Package details"}),
                )
            )
            read = json.loads(
                await execute_tool(
                    catalog,
                    "whatsapp_read_current_customer_chat",
                    json.dumps({"limit": 8}),
                )
            )
            replied = json.loads(
                await execute_tool(
                    catalog,
                    "whatsapp_reply_current_customer",
                    json.dumps({"message_id": "incoming-1", "text": "Here is the answer"}),
                )
            )
        finally:
            await runtime.close()

    assert calls == [
        ("send-text", {"chatId": CHAT_ID, "text": "Package details"}),
        (
            "reply",
            {
                "chatId": CHAT_ID,
                "quotedMessageId": "incoming-1",
                "text": "Here is the answer",
            },
        ),
    ]
    assert sent["message_id"] == "msg-1"
    assert sent["delivery_status"] == "accepted"
    assert sent["delivery_confirmed"] is False
    assert read["messages"][0]["body"] == "Hello"
    assert replied["message_id"] == "reply-1"


@pytest.mark.asyncio
async def test_media_requires_operator_approved_https_host() -> None:
    runtime = OpenWAToolRuntime(
        config("http://127.0.0.1:2785", "whatsapp_send_media_current_customer"),
        caller_id=CALLER,
        task_id="iptv",
        call_id="call-2",
    )
    with pytest.raises(OpenWAError, match="not approved"):
        runtime._approved_media_url("https://attacker.example/file.pdf")
    assert runtime._approved_media_url("https://files.cdn.example.com/file.pdf").startswith(
        "https://"
    )


def test_send_text_tool_requires_exact_dictated_wording() -> None:
    description = OpenWAToolRuntime._definitions()[
        "whatsapp_send_text_current_customer"
    ]["description"]
    assert "preserve every word exactly" in description


@pytest.mark.asyncio
async def test_per_use_approval_blocks_send_before_openwa_request(tmp_path: Path) -> None:
    invoked = asyncio.Event()

    async def check(request: web.Request) -> web.Response:
        return web.json_response({"exists": True, "whatsappId": CHAT_ID})

    async def send(request: web.Request) -> web.Response:
        invoked.set()
        return web.json_response({"messageId": "approved-message"}, status=201)

    app = web.Application()
    app.router.add_get("/api/sessions/{session}/contacts/check/{number}", check)
    app.router.add_post("/api/sessions/{session}/messages/send-text", send)
    queue = ToolApprovalQueue(tmp_path / "approvals")
    async with TestServer(app) as upstream:
        value = config(
            str(upstream.make_url("/"))[:-1], "whatsapp_send_text_current_customer"
        )
        policies = [
            policy.model_copy(
                update={"approval_mode": "per_use"}
                if policy.name == "whatsapp_send_text_current_customer"
                else {}
            )
            for policy in value.tools
        ]
        value = value.model_copy(update={"tools": policies})
        runtime = OpenWAToolRuntime(
            value,
            caller_id=CALLER,
            task_id="iptv",
            call_id="call-approval",
            approval_queue=queue,
        )
        try:
            catalog = await runtime.start()
            pending_call = asyncio.create_task(
                execute_tool(
                    catalog,
                    "whatsapp_send_text_current_customer",
                    json.dumps({"text": "Approved"}),
                )
            )
            for _ in range(80):
                pending = queue.list_active()
                if pending:
                    break
                await asyncio.sleep(0.025)
            assert invoked.is_set() is False
            queue.decide(pending[0]["request_id"], approved=True)
            result = json.loads(await asyncio.wait_for(pending_call, timeout=3))
        finally:
            await runtime.close()
    assert invoked.is_set() is True
    assert result["message_id"] == "approved-message"


@pytest.mark.asyncio
async def test_event_bridge_filters_other_customers_deduplicates_and_tracks_ack() -> None:
    conversations: list[tuple[str, bool]] = []
    events: list[dict] = []

    async def conversation(text: str, respond: bool) -> None:
        conversations.append((text, respond))

    bridge = OpenWAEventBridge(
        OpenWAConfig(api_key="key", session_id=SESSION_ID),
        phone_digits="212600123456",
        event_sink=events.append,
        conversation_sink=conversation,
    )
    incoming = {
        "type": "event",
        "payload": {
            "event": "message.received",
            "sessionId": SESSION_ID,
            "data": {
                "id": "customer-message-1",
                "from": CHAT_ID,
                "body": "Please send the link",
                "type": "text",
                "fromMe": False,
            },
        },
    }
    await bridge._handle_envelope(incoming)
    await bridge._handle_envelope(incoming)
    other = json.loads(json.dumps(incoming))
    other["payload"]["data"]["id"] = "other-message"
    other["payload"]["data"]["from"] = "212699999999@c.us"
    await bridge._handle_envelope(other)
    bridge.sent_message_ids.add("sent-1")
    await bridge._handle_envelope(
        {
            "type": "event",
            "payload": {
                "event": "message.ack",
                "sessionId": SESSION_ID,
                "data": {"messageId": "sent-1", "status": "delivered"},
            },
        }
    )

    assert len([event for event in events if event["type"] == "openwa_customer_message"]) == 1
    assert len(conversations) == 2
    assert conversations[0][1] is True
    assert "UNTRUSTED WHATSAPP" in conversations[0][0]
    assert conversations[1][1] is False
    assert bridge.delivery_status["sent-1"] == "delivered"


@pytest.mark.asyncio
async def test_delivery_ack_arriving_before_send_registration_is_not_lost() -> None:
    bridge = OpenWAEventBridge(
        OpenWAConfig(api_key="key", session_id=SESSION_ID),
        phone_digits="212600123456",
    )

    await bridge._delivery_ack({"messageId": "fast-ack", "status": "delivered"})
    assert "fast-ack" not in bridge.delivery_status

    await bridge.register_sent_message("fast-ack")

    assert await bridge.wait_for_delivery("fast-ack", 0.1) == "delivered"


@pytest.mark.asyncio
async def test_delivery_wait_continues_past_server_ack_until_device_delivery() -> None:
    bridge = OpenWAEventBridge(
        OpenWAConfig(api_key="key", session_id=SESSION_ID),
        phone_digits="212600123456",
    )
    await bridge.register_sent_message("sent-2")
    waiting = asyncio.create_task(bridge.wait_for_delivery("sent-2", 1.0))

    await bridge._delivery_ack({"messageId": "sent-2", "ack": 1})
    await asyncio.sleep(0)
    assert waiting.done() is False
    await bridge._delivery_ack({"messageId": "sent-2", "ack": 2})

    assert await waiting == "delivered"


@pytest.mark.asyncio
async def test_send_tool_returns_verified_delivery_with_bounded_wait() -> None:
    value = config("http://127.0.0.1:2785", "whatsapp_send_text_current_customer")
    value = value.model_copy(update={"delivery_confirmation_timeout_ms": 500})
    runtime = OpenWAToolRuntime(
        value,
        caller_id=CALLER,
        task_id="iptv",
        call_id="delivery-confirmation",
    )
    runtime.event_bridge = OpenWAEventBridge(
        value,
        phone_digits="212600123456",
    )

    async def acknowledge() -> None:
        await asyncio.sleep(0.02)
        assert runtime.event_bridge is not None
        await runtime.event_bridge._delivery_ack(
            {"messageId": "confirmed-message", "status": "delivered"}
        )

    ack_task = asyncio.create_task(acknowledge())
    result = await runtime._sent({"messageId": "confirmed-message"}, "text")
    await ack_task

    assert result["delivery_confirmed"] is True
    assert result["delivery_status"] == "delivered"
    assert result["delivery_wait_ms"] < 500


@pytest.mark.asyncio
async def test_send_tool_stops_waiting_at_configured_delivery_timeout() -> None:
    value = config("http://127.0.0.1:2785", "whatsapp_send_text_current_customer")
    value = value.model_copy(update={"delivery_confirmation_timeout_ms": 10})
    runtime = OpenWAToolRuntime(
        value,
        caller_id=CALLER,
        task_id="iptv",
        call_id="delivery-timeout",
    )
    runtime.event_bridge = OpenWAEventBridge(
        value,
        phone_digits="212600123456",
    )

    result = await runtime._sent({"messageId": "pending-message"}, "text")

    assert result["delivery_confirmed"] is False
    assert result["delivery_status"] == "accepted"
    assert 5 <= result["delivery_wait_ms"] < 250


@pytest.mark.asyncio
async def test_self_chat_uses_history_confirmation_without_claiming_delivery() -> None:
    class HistoryClient:
        async def history(self, chat_id: str, limit: int) -> list[dict]:
            assert chat_id == CHAT_ID
            assert limit == 20
            return [
                {
                    "id": "database-row-id",
                    "waMessageId": "self-message",
                    "body": "Connection is good",
                }
            ]

    value = config("http://127.0.0.1:2785", "whatsapp_send_text_current_customer")
    runtime = OpenWAToolRuntime(
        value,
        caller_id=CALLER,
        task_id="iptv",
        call_id="self-chat-confirmation",
    )
    runtime.client = HistoryClient()  # type: ignore[assignment]
    runtime.event_bridge = OpenWAEventBridge(value, phone_digits="212600123456")
    runtime._self_chat = True

    result = await runtime._sent(
        {"messageId": "self-message"}, "text", chat_id=CHAT_ID
    )

    assert result["chat_confirmed"] is True
    assert result["delivery_confirmed"] is False
    assert result["confirmation_status"] == "confirmed_in_chat"
    assert "do not call it device delivered" in result["guidance"]


@pytest.mark.asyncio
async def test_other_numbers_never_use_self_chat_history_fallback() -> None:
    class ForbiddenHistoryClient:
        async def history(self, _chat_id: str, _limit: int) -> list[dict]:
            raise AssertionError("history fallback must not run for another number")

    value = config("http://127.0.0.1:2785", "whatsapp_send_text_current_customer")
    value = value.model_copy(update={"delivery_confirmation_timeout_ms": 10})
    runtime = OpenWAToolRuntime(
        value,
        caller_id=CALLER,
        task_id="iptv",
        call_id="normal-delivery",
    )
    runtime.client = ForbiddenHistoryClient()  # type: ignore[assignment]
    runtime.event_bridge = OpenWAEventBridge(value, phone_digits="212600123456")
    runtime._self_chat = False

    result = await runtime._sent(
        {"messageId": "normal-message"}, "text", chat_id=CHAT_ID
    )

    assert result["chat_confirmed"] is False
    assert result["delivery_status"] == "accepted"


def test_openwa_session_phone_number_detection_is_bounded() -> None:
    assert OpenWAToolRuntime._session_phone_digits({"phone": "+212 600-000000"}) == (
        "212600000000"
    )
    assert OpenWAToolRuntime._session_phone_digits(
        {"me": {"id": "212600123456:7@s.whatsapp.net"}}
    ) == (
        "212600123456"
    )


@pytest.mark.asyncio
async def test_event_bridge_reports_subscription_errors_and_retries_initial_failure() -> None:
    events: list[dict] = []
    bridge = OpenWAEventBridge(
        OpenWAConfig(api_key="key", session_id=SESSION_ID, request_timeout_ms=500),
        phone_digits="212600123456",
        event_sink=events.append,
    )

    await bridge._handle_envelope(
        {
            "type": "subscribed",
            "events": ["message.received", "message.ack"],
        }
    )
    await bridge._handle_envelope(
        {"type": "error", "code": "FORBIDDEN_SESSION", "message": "not allowed"}
    )
    assert events[0]["state"] == "subscribed"
    assert events[1]["state"] == "error"

    class ReconnectingClient:
        connected = False

        def __init__(self) -> None:
            self.connect_calls = 0

        async def connect(self, *args: object, **kwargs: object) -> None:
            self.connect_calls += 1
            if self.connect_calls == 1:
                raise ConnectionError("temporary outage")
            bridge._closing.set()

        async def wait(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.connected = False

    client = ReconnectingClient()
    bridge.client = client  # type: ignore[assignment]
    await asyncio.wait_for(bridge._run(), timeout=2)
    assert client.connect_calls == 2
    assert any(event.get("retrying") is True for event in events)


@pytest.mark.asyncio
async def test_web_api_tests_lists_and_provisions_dedicated_key(tmp_path: Path) -> None:
    async def ready(request: web.Request) -> web.Response:
        return web.json_response({"status": "ready"})

    async def session_status(request: web.Request) -> web.Response:
        return web.json_response(
            {"id": SESSION_ID, "name": "main", "status": "ready", "isActive": True}
        )

    async def sessions(request: web.Request) -> web.Response:
        return web.json_response(
            [{"id": SESSION_ID, "name": "main", "status": "ready", "isActive": True}]
        )

    async def provision(request: web.Request) -> web.Response:
        assert request.headers["X-API-Key"] == "master-key"
        payload = await request.json()
        assert payload["allowedSessions"] == [SESSION_ID]
        return web.json_response(
            {
                "id": "key-id",
                "keyPrefix": "owa_phone",
                "apiKey": "dedicated-phoneagent-key",
            },
            status=201,
        )

    upstream_app = web.Application()
    upstream_app.router.add_get("/api/health/ready", ready)
    upstream_app.router.add_get("/api/sessions", sessions)
    upstream_app.router.add_get("/api/sessions/{session}", session_status)
    upstream_app.router.add_post("/api/auth/api-keys", provision)
    async with TestServer(upstream_app) as upstream:
        store = OpenWAConfigStore(tmp_path / "openwa.json")
        server = PhoneAgentWebServer(
            config=ProviderConfig(
                stt_provider="parakeet_local",
                llm_provider="ollama",
                tts_provider="edge_tts",
                tts_voice_id="en-US-AndrewMultilingualNeural",
                stt_language="en-US",
            ),
            openwa_config_store=store,
            audit_ledger=AuditLedger(tmp_path / "audit.jsonl"),
        )
        base_url = str(upstream.make_url("/"))[:-1]
        async with TestClient(TestServer(server.app)) as client:
            listed = await client.post(
                "/api/openwa/sessions",
                json={"base_url": base_url, "admin_key": "master-key"},
            )
            assert listed.status == 200
            created = await client.post(
                "/api/openwa/provision",
                json={
                    "base_url": base_url,
                    "admin_key": "master-key",
                    "session_id": SESSION_ID,
                },
            )
            created_body = await created.json()
            assert created.status == 200
            assert created_body["config"]["api_key"] == MASKED_SECRET
            tested = await client.post(
                "/api/openwa/test", json={"config": created_body["config"]}
            )
            assert tested.status == 200

    assert store.load().api_key == "dedicated-phoneagent-key"
    assert "master-key" not in store.path.read_text()
    assert "openwa_key_provisioned" in (tmp_path / "audit.jsonl").read_text()


@pytest.mark.asyncio
async def test_web_api_does_not_report_qr_ready_session_as_connected(tmp_path: Path) -> None:
    async def ready(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def session_status(request: web.Request) -> web.Response:
        return web.json_response(
            {"id": SESSION_ID, "name": "pair-me", "status": "qr_ready"}
        )

    upstream_app = web.Application()
    upstream_app.router.add_get("/api/health/ready", ready)
    upstream_app.router.add_get("/api/sessions/{session}", session_status)
    async with TestServer(upstream_app) as upstream:
        store = OpenWAConfigStore(tmp_path / "openwa.json")
        store.save(
            OpenWAConfig(
                base_url=str(upstream.make_url("/"))[:-1],
                api_key="dedicated",
                session_id=SESSION_ID,
            ).model_dump(mode="json")
        )
        server = PhoneAgentWebServer(openwa_config_store=store)
        async with TestClient(TestServer(server.app)) as client:
            state = await (await client.get("/api/openwa")).json()
            tested = await client.post(
                "/api/openwa/test", json={"config": state["config"]}
            )
            body = await tested.json()

    assert state["connectivity"]["session_ready"] is False
    assert tested.status == 409
    assert body["status"] == "not_ready"
    assert "pair or reconnect" in body["message"]
