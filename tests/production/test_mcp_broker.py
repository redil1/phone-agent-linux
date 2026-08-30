from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from phone_agent_gateway.ai_bridge.mcp_broker import (
    McpBrokerError,
    McpServerConfig,
    McpToolBroker,
    _sanitize,
    _validate_schema_definition,
    _validate_value,
    load_mcp_config,
)
from phone_agent_gateway.ai_bridge.tasks.tool_catalog import execute_tool


def test_configuration_is_exact_bounded_and_argv_only(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": [
                    {
                        "label": "inventory",
                        "command": [sys.executable, "-c", "print('ok')"],
                        "allowed_tools": ["lookup"],
                    }
                ],
            }
        )
    )
    config.chmod(0o600)
    loaded = load_mcp_config(config)
    assert loaded[0].label == "inventory"
    assert loaded[0].command[0] == str(Path(sys.executable).resolve())

    payload = json.loads(config.read_text())
    payload["servers"][0]["shell"] = True
    config.write_text(json.dumps(payload))
    config.chmod(0o600)
    with pytest.raises(McpBrokerError, match="unknown fields"):
        load_mcp_config(config)
    config.chmod(0o666)
    with pytest.raises(McpBrokerError, match="group/world writable"):
        load_mcp_config(config)


def test_schema_and_arguments_fail_closed() -> None:
    schema = _validate_schema_definition(
        {
            "type": "object",
            "properties": {"sku": {"type": "string", "enum": ["demo-1"]}},
            "required": ["sku"],
            "additionalProperties": False,
        }
    )
    _validate_value({"sku": "demo-1"}, schema)
    with pytest.raises(McpBrokerError):
        _validate_value({"sku": "demo-1", "secret": "x"}, schema)
    # Official MCP SDKs often omit additionalProperties. PhoneAgent accepts
    # that metadata but tightens the model-facing schema to false.
    tightened = _validate_schema_definition({"type": "object", "properties": {}})
    assert tightened["additionalProperties"] is False
    optional = _validate_schema_definition(
        {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    )
    _validate_value("value", optional)
    _validate_value(None, optional)
    with pytest.raises(McpBrokerError):
        _validate_value(3, optional)


def test_output_redacts_secret_email_and_phone() -> None:
    safe = _sanitize(
        {
            "access_token": "private",
            "message": "write me at a@example.com or +212 600 000 000",
        }
    )
    assert safe["access_token"] == "<redacted>"
    assert "a@example.com" not in safe["message"]
    assert "600 000 000" not in safe["message"]


@pytest.mark.asyncio
async def test_stdio_discovery_allowlist_call_and_mutation_gate(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(
        """
import json, sys
for line in sys.stdin:
    req=json.loads(line); method=req.get('method'); rid=req.get('id')
    if method=='initialize':
        result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'test','version':'1'}}
    elif method=='tools/list':
        schema={'type':'object','properties':{'sku':{'type':'string','enum':['demo-1']}},'required':['sku'],'additionalProperties':False}
        result={'tools':[
            {'name':'lookup','description':'Read inventory.','inputSchema':schema,
             'annotations':{'readOnlyHint':True}},
            {'name':'write','description':'Change inventory.','inputSchema':schema,
             'annotations':{'readOnlyHint':False}},
            {'name':'hidden','description':'Hidden.','inputSchema':schema,
             'annotations':{'readOnlyHint':True}}
        ]}
    elif method=='tools/call':
        result={'structuredContent':{'available':True,'sku':req['params']['arguments']['sku']},'content':[],'isError':False}
    else: result={}
    if rid is not None: print(json.dumps({'jsonrpc':'2.0','id':rid,'result':result}),flush=True)
"""
    )
    config = McpServerConfig(
        label="inventory",
        command=(sys.executable, "-u", str(server)),
        allowed_tools=frozenset({"lookup", "write"}),
        timeout_secs=1,
    )
    broker = McpToolBroker(
        (config,),
        task_allowed_tools={"mcp_inventory__lookup", "mcp_inventory__write"},
        call_id="call-1",
    )
    tools = await broker.start()
    assert set(tools) == {"mcp_inventory__lookup", "mcp_inventory__write"}
    read = json.loads(await execute_tool(tools, "mcp_inventory__lookup", '{"sku":"demo-1"}'))
    assert read["structuredContent"]["available"] is True
    write = json.loads(await execute_tool(tools, "mcp_inventory__write", '{"sku":"demo-1"}'))
    assert write["completed"] is False
    assert write["reason"] == "operator_approval_required"
    await broker.close()
