from __future__ import annotations

import asyncio
import json
import socket
import stat
import sys
from pathlib import Path

import pytest
import uvicorn
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from mcp.server.fastmcp import FastMCP

from phone_agent_gateway.ai_bridge.production_security import AuditLedger
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
from phone_agent_gateway.ai_bridge.tool_control import (
    MASKED_SECRET,
    ManagedToolPolicy,
    ManagedToolRuntime,
    ToolApprovalQueue,
    ToolConnection,
    ToolControlConfig,
    ToolControlStore,
    discover_connection,
)
from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer


def schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 2,
                "maxLength": 200,
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }


def http_connection(url: str, *, approval: str = "never") -> ToolConnection:
    return ToolConnection(
        id="internet_search",
        label="SearXNG",
        kind="http",
        enabled=True,
        url=url,
        allow_insecure_http=True,
        argument_map={"query": "q"},
        static_parameters={"format": "json"},
        response_path="results",
        max_results=2,
        tools=[
            ManagedToolPolicy(
                source_name="internet_search",
                exposed_name="internet_search",
                description="Search current public information.",
                input_schema=schema(),
                enabled=True,
                approval_mode=approval,
                task_ids=["iptv"],
                read_only=True,
            )
        ],
    )


def test_private_store_masks_and_preserves_headers(tmp_path: Path) -> None:
    store = ToolControlStore(tmp_path / "tool-control.json")
    connection = http_connection("https://search.example/search")
    connection.headers = {"Authorization": "Bearer private-value"}
    saved = store.save(ToolControlConfig(connections=[connection]).model_dump(mode="json"))

    assert saved.revision == 1
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    public = store.public_state()
    assert public["connections"][0]["headers"]["Authorization"] == MASKED_SECRET
    assert "private-value" not in json.dumps(public)

    # Saving the browser's masked representation keeps the secret rather than
    # replacing it with bullets or forcing the operator to re-enter it.
    again = store.save(public)
    assert again.connections[0].headers["Authorization"] == "Bearer private-value"
    assert again.revision == 2


def test_plain_http_requires_explicit_operator_activation() -> None:
    with pytest.raises(ValueError, match="plain HTTP"):
        http_connection("http://search.example/search").model_copy(
            update={"allow_insecure_http": False}
        ).model_dump()
        ToolConnection.model_validate(
            {
                **http_connection("http://search.example/search").model_dump(),
                "allow_insecure_http": False,
            }
        )


@pytest.mark.asyncio
async def test_http_tool_executes_bounded_sanitized_search() -> None:
    async def search(request: web.Request) -> web.Response:
        assert request.query["q"] == "Berlin weather"
        assert request.query["format"] == "json"
        return web.json_response(
            {
                "results": [
                    {
                        "title": "Weather one",
                        "url": "https://weather.example/one",
                        "content": "Contact a@example.com or +212 600 000 000",
                    },
                    {"title": "Weather two", "url": "https://weather.example/two"},
                    {"title": "Ignored third", "url": "https://weather.example/three"},
                ]
            }
        )

    app = web.Application()
    app.router.add_get("/search", search)
    async with TestServer(app) as server:
        connection = http_connection(str(server.make_url("/search")))
        runtime = ManagedToolRuntime(
            ToolControlConfig(connections=[connection]),
            task_id="iptv",
            call_id="call-1",
        )
        try:
            assert set(await runtime.start()) == {"internet_search"}
            result = await runtime.execute_for_test(
                "internet_search", {"query": "Berlin weather"}
            )
        finally:
            await runtime.close()

    assert result["ok"] is True
    assert len(result["results"]) == 2
    rendered = json.dumps(result)
    assert "a@example.com" not in rendered
    assert "600 000 000" not in rendered


@pytest.mark.asyncio
async def test_per_use_approval_blocks_until_exact_operator_decision(tmp_path: Path) -> None:
    invoked = asyncio.Event()

    async def search(request: web.Request) -> web.Response:
        invoked.set()
        return web.json_response({"results": [{"title": "Approved"}]})

    app = web.Application()
    app.router.add_get("/search", search)
    queue = ToolApprovalQueue(tmp_path / "approvals")
    events: list[dict] = []

    async with TestServer(app) as server:
        connection = http_connection(str(server.make_url("/search")), approval="per_use")
        runtime = ManagedToolRuntime(
            ToolControlConfig(connections=[connection]),
            task_id="iptv",
            call_id="call-approval",
            approval_queue=queue,
            event_sink=events.append,
        )
        try:
            await runtime.start()
            execution = asyncio.create_task(
                runtime.execute_for_test("internet_search", {"query": "approved search"})
            )
            for _ in range(40):
                pending = queue.list_active()
                if pending:
                    break
                await asyncio.sleep(0.025)
            assert pending[0]["state"] == "pending"
            assert invoked.is_set() is False
            queue.decide(pending[0]["request_id"], approved=True)
            result = await asyncio.wait_for(execution, timeout=3)
        finally:
            await runtime.close()

    assert invoked.is_set() is True
    assert result["results"][0]["title"] == "Approved"
    assert events[0]["type"] == "tool_approval_required"


@pytest.mark.asyncio
async def test_stdio_mcp_discovery_defaults_mutations_to_per_use_approval(
    tmp_path: Path,
) -> None:
    server = tmp_path / "mcp_server.py"
    server.write_text(
        """
import json, sys
for line in sys.stdin:
    request=json.loads(line); method=request.get('method'); rid=request.get('id')
    if method=='initialize':
        result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'test','version':'1'}}
    elif method=='tools/list':
        schema={'type':'object','properties':{},'required':[],'additionalProperties':False}
        result={'tools':[
            {'name':'read','description':'Read data.','inputSchema':schema,
             'annotations':{'readOnlyHint':True}},
            {'name':'write','description':'Write data.','inputSchema':schema,
             'annotations':{'readOnlyHint':False}}
        ]}
    else: result={}
    if rid is not None: print(json.dumps({'jsonrpc':'2.0','id':rid,'result':result}),flush=True)
""",
        encoding="utf-8",
    )
    connection = ToolConnection(
        id="inventory",
        label="Inventory",
        kind="mcp_stdio",
        command=[sys.executable, "-u", str(server)],
    )

    discovered = await discover_connection(connection)

    assert [tool.source_name for tool in discovered.tools] == ["read", "write"]
    assert discovered.tools[0].approval_mode == "never"
    assert discovered.tools[1].approval_mode == "per_use"
    assert all(tool.enabled is False for tool in discovered.tools)


@pytest.mark.asyncio
async def test_streamable_http_mcp_discovery_execution_and_clean_shutdown() -> None:
    mcp = FastMCP(
        "remote-test",
        stateless_http=True,
        json_response=True,
        log_level="ERROR",
    )

    @mcp.tool()
    async def echo(query: str) -> dict:
        """Echo a query."""

        return {"echo": query}

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(mcp.streamable_http_app(), log_level="error", lifespan="on")
    )
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    runtime: ManagedToolRuntime | None = None
    try:
        connection = ToolConnection(
            id="remote_mcp",
            label="Remote MCP",
            kind="mcp_http",
            url=f"http://127.0.0.1:{sock.getsockname()[1]}/mcp",
            allow_insecure_http=True,
        )
        discovered = await discover_connection(connection)
        policy = discovered.tools[0].model_copy(
            update={"enabled": True, "approval_mode": "never", "task_ids": ["iptv"]}
        )
        active = discovered.model_copy(update={"enabled": True, "tools": [policy]})
        runtime = ManagedToolRuntime(
            ToolControlConfig(connections=[active]),
            task_id="iptv",
            call_id="remote-call",
        )
        assert set(await runtime.start()) == {"mcp_remote_mcp__echo"}
        result = await runtime.execute_for_test(
            "mcp_remote_mcp__echo", {"query": "hello"}
        )
        assert "hello" in json.dumps(result)
    finally:
        if runtime is not None:
            await runtime.close()
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_studio_api_saves_tests_and_decides_managed_tools(tmp_path: Path) -> None:
    async def search(request: web.Request) -> web.Response:
        return web.json_response({"results": [{"title": request.query["q"]}]})

    app = web.Application()
    app.router.add_get("/search", search)
    async with TestServer(app) as search_server:
        store = ToolControlStore(tmp_path / "tool-control.json")
        approvals = ToolApprovalQueue(tmp_path / "approvals")
        server = PhoneAgentWebServer(
            config=ProviderConfig(
                stt_provider="parakeet_local",
                llm_provider="ollama",
                tts_provider="edge_tts",
                tts_voice_id="en-US-AndrewMultilingualNeural",
                stt_language="en-US",
            ),
            tool_control_store=store,
            tool_approval_queue=approvals,
            audit_ledger=AuditLedger(tmp_path / "audit.jsonl"),
        )
        environment = server._child_environment()
        assert environment["PHONE_AGENT_TOOL_CONTROL"] == str(store.path)
        assert environment["PHONE_AGENT_TOOL_APPROVAL_DIR"] == str(approvals.directory)
        connection = http_connection(str(search_server.make_url("/search")))
        config = ToolControlConfig(connections=[connection]).model_dump(mode="json")
        async with TestClient(TestServer(server.app)) as client:
            saved_response = await client.post("/api/tools", json={"config": config})
            saved = await saved_response.json()
            assert saved_response.status == 200
            assert saved["active_tools"] == ["internet_search"]

            tested_response = await client.post(
                "/api/tools/test",
                json={
                    "connection": saved["config"]["connections"][0],
                    "arguments": {"query": "Studio search"},
                },
            )
            tested = await tested_response.json()
            assert tested_response.status == 200
            assert tested["result"]["results"][0]["title"] == "Studio search"

            request = approvals.create(
                tool_name="external_write",
                arguments={"value": "one"},
                call_id_hash="abc",
                timeout_seconds=30,
            )
            approval_response = await client.post(
                "/api/tools/approvals/decide",
                json={"request_id": request["request_id"], "approved": False},
            )
            assert approval_response.status == 200
            assert approvals.read(request["request_id"])["state"] == "rejected"

    assert "tool_control_updated" in (tmp_path / "audit.jsonl").read_text()
    assert "tool_approval_decided" in (tmp_path / "audit.jsonl").read_text()
