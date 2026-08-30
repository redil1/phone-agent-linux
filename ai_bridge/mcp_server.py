"""Local stdio MCP server for controlling PhoneAgent through approved actions."""

from __future__ import annotations

import json
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .local_control import LocalControlError, local_control_request

PROTOCOL_VERSION = "2024-11-05"
try:
    PACKAGE_VERSION = version("phone-agent-gateway")
except PackageNotFoundError:
    PACKAGE_VERSION = "0.7.0"
SERVER_INFO = {"name": "phone-agent-local", "version": PACKAGE_VERSION}
EMPTY_OBJECT = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
PACKAGE_OBJECT = {
    "type": "object",
    "description": (
        "A complete AgentPackage matching phoneagent://schema/agent-package. "
        "Read the schema resource before constructing this object."
    ),
    "additionalProperties": True,
}
TOOLS = [
    {
        "name": "phone_agent_status",
        "description": "Read the current PhoneAgent call and channel status.",
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_capabilities",
        "description": "Read the locally available PhoneAgent channels and safety constraints.",
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_identity",
        "description": (
            "Read the active PhoneAgent identity version, role, mission, values, languages, "
            "trusted skills and evaluation status."
        ),
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_request_dial",
        "description": (
            "Request operator approval for an outbound call. This never places the call. "
            "Use the returned request_id only after the operator approves it in Studio."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "minLength": 8,
                    "maxLength": 32,
                    "description": "Telephone number to call.",
                },
                "recording_consent": {
                    "type": "boolean",
                    "description": "True only when all legally required consent was confirmed.",
                },
            },
            "required": ["destination", "recording_consent"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "phone_agent_execute_approved_dial",
        "description": (
            "Place a previously approved call using its one-time request_id. "
            "Fails unless the exact request was approved by the Studio operator."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "minLength": 16, "maxLength": 64}
            },
            "required": ["request_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "phone_agent_control_schema",
        "description": "Read the complete AgentPackage JSON Schema and immutable boundaries.",
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_get_active_package",
        "description": (
            "Read the complete effective PhoneAgent package with secrets masked. Clone this "
            "package, change only desired parameters, then validate and stage it."
        ),
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_validate_package",
        "description": (
            "Dry-run a complete AgentPackage against identity, task, runtime, tool, memory and "
            "integration contracts. This never changes active behavior."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"package": PACKAGE_OBJECT},
            "required": ["package"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_stage_package",
        "description": (
            "Stage an exactly validated AgentPackage against the current effective-state hash. "
            "Staging does not activate it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": PACKAGE_OBJECT,
                "reason": {"type": "string", "minLength": 3, "maxLength": 1000},
                "created_by": {"type": "string", "minLength": 2, "maxLength": 120},
            },
            "required": ["package", "reason", "created_by"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "phone_agent_list_deployments",
        "description": (
            "List recent staged, active, superseded and failed AgentPackage deployments."
        ),
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_activate_deployment",
        "description": (
            "Atomically activate a staged AgentPackage for future calls. Activation is refused "
            "during a call or after any stale configuration change."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deployment_id": {
                    "type": "string",
                    "pattern": "^dep_[a-f0-9]{24}$",
                }
            },
            "required": ["deployment_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_rollback_deployment",
        "description": "Stage and atomically reactivate a previous AgentPackage deployment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deployment_id": {"type": "string", "pattern": "^dep_[a-f0-9]{24}$"},
                "reason": {"type": "string", "minLength": 3, "maxLength": 1000},
                "created_by": {"type": "string", "minLength": 2, "maxLength": 120},
            },
            "required": ["deployment_id", "reason", "created_by"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_recent_events",
        "description": (
            "Read bounded live call, transcript, tool, deployment and diagnostic events after a "
            "sequence cursor. Caller routing identifiers are redacted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "after": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": [],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_list_tasks",
        "description": "Read all available task contracts and the currently active task.",
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_dial",
        "description": (
            "Place an administrator-authorized call using the active AgentPackage. Normal "
            "destination, consent, rate, cooldown, hardware and one-call policies still apply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "minLength": 8, "maxLength": 32},
                "recording_consent": {"type": "boolean"},
            },
            "required": ["destination", "recording_consent"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "phone_agent_hangup",
        "description": "End the currently owned call and return PhoneAgent to inbound listening.",
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    # ---- full configuration control -------------------------------------
    # An external agent that can only dial cannot actually run this appliance:
    # choosing the model, activating a tool, editing the persona or attaching a
    # handset all had to be done by hand in Studio. These expose the same
    # operations the UI performs, through the same audited endpoints.
    {
        "name": "phone_agent_get_configuration",
        "description": (
            "Read the full runtime configuration: pipeline mode, LLM, STT and TTS "
            "providers, models, voices and latency settings."
        ),
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_set_configuration",
        "description": (
            "Change runtime configuration. Accepts any subset of the fields returned "
            "by phone_agent_get_configuration, for example pipeline_mode, llm_provider, "
            "llm_model, stt_provider, tts_provider, tts_model, tts_voice_id. Applies to "
            "the next call; a call in progress is not disturbed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "phone_agent_get_tool_control",
        "description": (
            "Read the Tools & MCP control plane: declared HTTP tools, local stdio and "
            "remote MCP connections, which are active, and their task assignments."
        ),
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_set_tool_control",
        "description": (
            "Replace the Tools & MCP configuration. Activation changes reach a call "
            "already in progress within about a second."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"config": {"type": "object"}},
            "required": ["config"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_get_persona",
        "description": "Read the agent persona: identity, voice, style and guardrails.",
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_set_persona",
        "description": "Replace the agent persona. Applies to the next call.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_set_task",
        "description": (
            "Create or update a task contract: its goal, slots to collect, allowed "
            "tools, approval requirements and stop conditions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_delete_task",
        "description": "Delete a user-authored task contract by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_get_integration",
        "description": (
            "Read one business integration's configuration: 'frappe' for CRM and "
            "ERPNext, 'openwa' for WhatsApp messaging, or 'web-research'. Secrets "
            "come back masked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "integration": {
                    "type": "string",
                    "enum": ["frappe", "openwa", "web-research"],
                }
            },
            "required": ["integration"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_set_integration",
        "description": (
            "Configure one business integration ('frappe', 'openwa' or "
            "'web-research'). The config object is that integration's own shape, as "
            "returned by phone_agent_get_integration."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "integration": {
                    "type": "string",
                    "enum": ["frappe", "openwa", "web-research"],
                },
                "config": {"type": "object"},
            },
            "required": ["integration", "config"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_test_integration",
        "description": (
            "Test connectivity for one integration without changing anything. Use "
            "this after configuring, before relying on it during a call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "integration": {
                    "type": "string",
                    "enum": ["frappe", "openwa", "web-research", "tools"],
                }
            },
            "required": ["integration"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_set_remote_link",
        "description": (
            "Enable or disable the cable-free link, which lets a handset reach this "
            "runtime over the network instead of USB."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            },
            "required": ["enabled"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_pairing_code",
        "description": (
            "Produce pairing material for a handset: an SVG QR carrying the link key, "
            "address and port, plus a short fingerprint to compare on the phone. "
            "Rotating invalidates the existing pairing, including over USB, until the "
            "phone is paired again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rotate": {"type": "boolean"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_list_approvals",
        "description": "List pending operator approvals, for dialing and for tool use.",
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_decide_approval",
        "description": "Approve or reject one pending approval by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "approved": {"type": "boolean"},
            },
            "required": ["request_id", "approved"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "phone_agent_get_evaluation",
        "description": "Read per-turn persona fidelity and task evaluation results.",
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "phone_agent_get_caller_memory",
        "description": "Read stored caller memory.",
        "inputSchema": EMPTY_OBJECT,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]

RESOURCES = [
    {
        "uri": "phoneagent://schema/agent-package",
        "name": "AgentPackage schema",
        "description": "Current JSON Schema and immutable PhoneAgent boundaries.",
        "mimeType": "application/json",
    },
    {
        "uri": "phoneagent://state/active-package",
        "name": "Active AgentPackage",
        "description": "Complete effective configuration with credentials masked.",
        "mimeType": "application/json",
    },
    {
        "uri": "phoneagent://state/capabilities",
        "name": "PhoneAgent capabilities",
        "description": "Available channels, identity features and protected constraints.",
        "mimeType": "application/json",
    },
]


def _tool_result(value: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": value,
        "isError": is_error,
    }


def _call_tool(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise LocalControlError("tool arguments must be an object")
    if name in {
        "phone_agent_status",
        "phone_agent_capabilities",
        "phone_agent_identity",
        "phone_agent_control_schema",
        "phone_agent_get_active_package",
        "phone_agent_list_deployments",
        "phone_agent_list_tasks",
        "phone_agent_hangup",
        "phone_agent_get_configuration",
        "phone_agent_get_tool_control",
        "phone_agent_get_persona",
        "phone_agent_list_approvals",
        "phone_agent_get_evaluation",
        "phone_agent_get_caller_memory",
    } and arguments:
        raise LocalControlError("this tool accepts no arguments")

    # ---- full configuration control -------------------------------------
    # Each of these is the endpoint Studio itself uses, so an external agent
    # and an operator cannot drift apart or bypass one another's guards.
    if name == "phone_agent_get_configuration":
        return local_control_request("GET", "/api/config")
    if name == "phone_agent_set_configuration":
        if not arguments:
            raise LocalControlError("set_configuration needs at least one field")
        return local_control_request("POST", "/api/config", payload=arguments)
    if name == "phone_agent_get_tool_control":
        return local_control_request("GET", "/api/tools")
    if name == "phone_agent_set_tool_control":
        if set(arguments) != {"config"} or not isinstance(arguments["config"], dict):
            raise LocalControlError("set_tool_control needs a config object")
        return local_control_request("POST", "/api/tools", payload=arguments)
    if name == "phone_agent_get_persona":
        return local_control_request("GET", "/api/persona")
    if name == "phone_agent_set_persona":
        if not arguments:
            raise LocalControlError("set_persona needs a persona body")
        return local_control_request("POST", "/api/persona", payload=arguments)
    if name == "phone_agent_set_task":
        if not arguments:
            raise LocalControlError("set_task needs a task contract")
        return local_control_request("POST", "/api/tasks", payload=arguments)
    if name == "phone_agent_delete_task":
        if set(arguments) != {"task_id"} or not isinstance(arguments["task_id"], str):
            raise LocalControlError("delete_task needs a task_id")
        return local_control_request("POST", "/api/tasks/delete", payload=arguments)
    if name in {
        "phone_agent_get_integration",
        "phone_agent_set_integration",
        "phone_agent_test_integration",
    }:
        integration = arguments.get("integration")
        if integration not in {"frappe", "openwa", "web-research", "tools"}:
            raise LocalControlError("unknown integration")
        if name == "phone_agent_get_integration":
            if set(arguments) != {"integration"}:
                raise LocalControlError("get_integration takes only an integration")
            return local_control_request("GET", f"/api/{integration}")
        if name == "phone_agent_test_integration":
            if set(arguments) != {"integration"}:
                raise LocalControlError("test_integration takes only an integration")
            return local_control_request("POST", f"/api/{integration}/test", payload={})
        if set(arguments) != {"integration", "config"} or not isinstance(
            arguments["config"], dict
        ):
            raise LocalControlError("set_integration needs an integration and a config")
        return local_control_request(
            "POST", f"/api/{integration}", payload=arguments["config"]
        )
    if name == "phone_agent_set_remote_link":
        if not isinstance(arguments.get("enabled"), bool):
            raise LocalControlError("set_remote_link needs enabled true or false")
        return local_control_request("POST", "/api/remote-link", payload=arguments)
    if name == "phone_agent_pairing_code":
        return local_control_request("POST", "/api/pairing", payload=arguments)
    if name == "phone_agent_list_approvals":
        return local_control_request("GET", "/api/approvals")
    if name == "phone_agent_decide_approval":
        if set(arguments) != {"request_id", "approved"}:
            raise LocalControlError("decide_approval needs request_id and approved")
        return local_control_request("POST", "/api/approvals/decide", payload=arguments)
    if name == "phone_agent_get_evaluation":
        return local_control_request("GET", "/api/eval")
    if name == "phone_agent_get_caller_memory":
        return local_control_request("GET", "/api/memory")

    if name == "phone_agent_status":
        return local_control_request("GET", "/api/mcp/status")
    if name == "phone_agent_capabilities":
        return local_control_request("GET", "/api/mcp/capabilities")
    if name == "phone_agent_identity":
        return local_control_request("GET", "/api/mcp/identity")
    if name == "phone_agent_control_schema":
        return local_control_request("GET", "/api/control/schema")
    if name == "phone_agent_get_active_package":
        return local_control_request("GET", "/api/control/package")
    if name == "phone_agent_list_deployments":
        return local_control_request("GET", "/api/control/deployments")
    if name == "phone_agent_list_tasks":
        return local_control_request("GET", "/api/tasks")
    if name == "phone_agent_hangup":
        return local_control_request("POST", "/api/control/hangup", payload={})
    if name == "phone_agent_validate_package":
        if set(arguments) != {"package"} or not isinstance(arguments["package"], dict):
            raise LocalControlError("validate_package arguments are invalid")
        return local_control_request("POST", "/api/control/validate", payload=arguments)
    if name == "phone_agent_stage_package":
        if set(arguments) != {"package", "reason", "created_by"}:
            raise LocalControlError("stage_package arguments are invalid")
        return local_control_request("POST", "/api/control/stage", payload=arguments)
    if name == "phone_agent_activate_deployment":
        if set(arguments) != {"deployment_id"}:
            raise LocalControlError("activate_deployment arguments are invalid")
        return local_control_request("POST", "/api/control/activate", payload=arguments)
    if name == "phone_agent_rollback_deployment":
        if set(arguments) != {"deployment_id", "reason", "created_by"}:
            raise LocalControlError("rollback_deployment arguments are invalid")
        return local_control_request("POST", "/api/control/rollback", payload=arguments)
    if name == "phone_agent_recent_events":
        if set(arguments) - {"after", "limit"}:
            raise LocalControlError("recent_events arguments are invalid")
        after = int(arguments.get("after", 0))
        limit = int(arguments.get("limit", 100))
        return local_control_request(
            "GET", f"/api/control/events?after={after}&limit={limit}"
        )
    if name == "phone_agent_dial":
        if set(arguments) != {"destination", "recording_consent"}:
            raise LocalControlError("dial arguments are invalid")
        return local_control_request("POST", "/api/control/dial", payload=arguments)
    if name == "phone_agent_request_dial":
        if set(arguments) != {"destination", "recording_consent"}:
            raise LocalControlError("request_dial arguments are invalid")
        if not isinstance(arguments["destination"], str) or not isinstance(
            arguments["recording_consent"], bool
        ):
            raise LocalControlError("request_dial arguments have invalid types")
        return local_control_request("POST", "/api/mcp/dial/request", payload=arguments)
    if name == "phone_agent_execute_approved_dial":
        if set(arguments) != {"request_id"} or not isinstance(arguments["request_id"], str):
            raise LocalControlError("execute_dial arguments are invalid")
        return local_control_request("POST", "/api/mcp/dial/execute", payload=arguments)
    raise LocalControlError("unknown tool")


def _handle(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return None
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": SERVER_INFO,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "resources/list":
            result = {"resources": RESOURCES}
        elif method == "resources/read":
            params = message.get("params")
            uri = str(params.get("uri") or "") if isinstance(params, dict) else ""
            mapping = {
                "phoneagent://schema/agent-package": "/api/control/schema",
                "phoneagent://state/active-package": "/api/control/package",
                "phoneagent://state/capabilities": "/api/mcp/capabilities",
            }
            path = mapping.get(uri)
            if path is None:
                raise LocalControlError("unknown resource URI")
            value = local_control_request("GET", path)
            result = {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    }
                ]
            }
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict):
                raise LocalControlError("tools/call params are invalid")
            result = _tool_result(
                _call_tool(str(params.get("name") or ""), params.get("arguments"))
            )
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except LocalControlError as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": _tool_result({"status": "error", "message": str(exc)}, is_error=True),
        }
    except Exception:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": _tool_result(
                {"status": "error", "message": "PhoneAgent tool failed"}, is_error=True
            ),
        }


def main() -> None:
    for line in sys.stdin.buffer:
        if len(line) > 512 * 1024:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle(message)
        if response is not None:
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
