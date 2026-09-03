"""Local-only MCP broker for OpenAI Realtime function tools.

OpenAI Realtime can call remote MCP servers directly, but PhoneAgent's private
tools stay local. This broker discovers stdio MCP tools, maps an explicitly
allowlisted subset to ordinary Realtime function definitions, executes calls
inside isolated subprocesses, and returns bounded/redacted JSON.

No MCP process can access a call merely because it is installed. A tool must be
allowed by both the active task contract and the server's local configuration.
Mutating tools are never executed automatically; their result is an approval
request until a separate operator approval system authorizes the exact action.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .tasks.tool_catalog import RealtimeTool
from .tasks.tool_registry import ToolSpec

logger = logging.getLogger("PhoneAgentMcpBroker")
try:
    PACKAGE_VERSION = version("phone-agent-gateway")
except PackageNotFoundError:
    PACKAGE_VERSION = "0.7.0"

DEFAULT_CONFIG = Path.home() / ".config" / "phone-agent" / "mcp_servers.json"
MAX_SERVERS = 8
MAX_TOOLS_PER_SERVER = 64
MAX_LINE_BYTES = 128 * 1024
MAX_DESCRIPTION_CHARS = 600
MAX_OUTPUT_BYTES = 8 * 1024
MAX_DEPTH = 8
LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
SAFE_ENV_NAMES = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR"})
SECRET_KEY_RE = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie)",
    re.I,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)")


class McpBrokerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    label: str
    command: tuple[str, ...]
    allowed_tools: frozenset[str]
    timeout_secs: float = 2.0
    max_output_bytes: int = MAX_OUTPUT_BYTES
    environment: tuple[str, ...] = ()


def _exact_keys(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    extras = set(value) - allowed
    if extras:
        raise McpBrokerError(f"{context} contains unknown fields: {', '.join(sorted(extras))}")


def load_mcp_config(path: Path | None = None) -> tuple[McpServerConfig, ...]:
    source = path or Path(os.getenv("PHONE_AGENT_MCP_CONFIG", "").strip() or DEFAULT_CONFIG)
    if not source.exists():
        return ()
    if not source.is_file() or source.is_symlink():
        raise McpBrokerError("MCP configuration must be a regular non-symlink file")
    metadata = source.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise McpBrokerError(
            "MCP configuration must be user-owned and not group/world writable"
        )
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpBrokerError("MCP configuration is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise McpBrokerError("MCP configuration must be an object")
    _exact_keys(payload, {"version", "servers"}, "MCP configuration")
    if payload.get("version") != 1 or not isinstance(payload.get("servers"), list):
        raise McpBrokerError("MCP configuration version/servers are invalid")
    if len(payload["servers"]) > MAX_SERVERS:
        raise McpBrokerError("too many MCP servers configured")
    configs: list[McpServerConfig] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload["servers"]):
        if not isinstance(raw, dict):
            raise McpBrokerError(f"MCP server {index} must be an object")
        _exact_keys(
            raw,
            {"label", "command", "allowed_tools", "timeout_ms", "max_output_bytes", "environment"},
            f"MCP server {index}",
        )
        label = str(raw.get("label") or "")
        if not LABEL_RE.fullmatch(label) or label in seen:
            raise McpBrokerError(f"MCP server {index} label is invalid or duplicated")
        seen.add(label)
        command_raw = raw.get("command")
        if not isinstance(command_raw, list) or not command_raw or len(command_raw) > 16:
            raise McpBrokerError(f"MCP server {label} command must be a bounded argv array")
        command = tuple(str(item) for item in command_raw)
        if any(not item or len(item) > 1_024 or "\x00" in item for item in command):
            raise McpBrokerError(f"MCP server {label} command contains an invalid argument")
        executable = Path(command[0]).expanduser()
        if not executable.is_absolute():
            discovered = shutil.which(command[0])
            if discovered is None:
                raise McpBrokerError(f"MCP server {label} executable was not found")
            executable = Path(discovered)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise McpBrokerError(f"MCP server {label} executable is not executable")
        command = (str(executable.resolve()), *command[1:])
        allowed_raw = raw.get("allowed_tools")
        if not isinstance(allowed_raw, list) or len(allowed_raw) > MAX_TOOLS_PER_SERVER:
            raise McpBrokerError(f"MCP server {label} allowed_tools must be a bounded list")
        allowed = frozenset(str(name) for name in allowed_raw)
        if any(not TOOL_RE.fullmatch(name) for name in allowed):
            raise McpBrokerError(f"MCP server {label} has an invalid allowed tool name")
        timeout_ms = raw.get("timeout_ms", 2_000)
        max_output = raw.get("max_output_bytes", MAX_OUTPUT_BYTES)
        if not isinstance(timeout_ms, int) or not 100 <= timeout_ms <= 10_000:
            raise McpBrokerError(f"MCP server {label} timeout_ms is invalid")
        if not isinstance(max_output, int) or not 256 <= max_output <= 64 * 1024:
            raise McpBrokerError(f"MCP server {label} max_output_bytes is invalid")
        environment_raw = raw.get("environment", [])
        if not isinstance(environment_raw, list) or len(environment_raw) > 16:
            raise McpBrokerError(f"MCP server {label} environment is invalid")
        environment = tuple(str(name) for name in environment_raw)
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) for name in environment):
            raise McpBrokerError(f"MCP server {label} environment name is invalid")
        configs.append(
            McpServerConfig(
                label=label,
                command=command,
                allowed_tools=allowed,
                timeout_secs=timeout_ms / 1_000,
                max_output_bytes=max_output,
                environment=environment,
            )
        )
    return tuple(configs)


def _validate_schema_definition(schema: Any, *, depth: int = 0) -> dict[str, Any]:
    if depth > MAX_DEPTH or not isinstance(schema, dict):
        raise McpBrokerError("MCP tool schema is invalid or too deeply nested")
    allowed = {
        "type", "properties", "required", "additionalProperties", "description", "enum",
        "items", "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems",
        "title", "default", "anyOf", "oneOf",
    }
    _exact_keys(schema, allowed, "MCP tool schema")
    kind = schema.get("type")
    union_key = "anyOf" if "anyOf" in schema else ("oneOf" if "oneOf" in schema else None)
    if union_key is not None:
        branches = schema[union_key]
        if kind is not None or not isinstance(branches, list) or not 1 <= len(branches) <= 4:
            raise McpBrokerError("MCP schema union is invalid")
        normalized_union: dict[str, Any] = {
            "anyOf": [
                _validate_schema_definition(branch, depth=depth + 1) for branch in branches
            ]
        }
        for key, limit in (("title", 200), ("description", MAX_DESCRIPTION_CHARS)):
            if key in schema:
                text = str(schema[key])
                if len(text) > limit:
                    raise McpBrokerError(f"MCP schema {key} is too long")
                normalized_union[key] = text
        if "default" in schema:
            default = schema["default"]
            if isinstance(default, dict | list) or (
                isinstance(default, str) and len(default) > 1_000
            ):
                raise McpBrokerError("MCP schema default is invalid")
            normalized_union["default"] = default
        return normalized_union
    if kind not in {"object", "string", "integer", "number", "boolean", "array", "null"}:
        raise McpBrokerError("MCP tool schema uses an unsupported type")
    normalized: dict[str, Any] = {"type": kind}
    if "title" in schema:
        title = str(schema["title"])
        if len(title) > 200:
            raise McpBrokerError("MCP schema title is too long")
        normalized["title"] = title
    if "description" in schema:
        description = str(schema["description"])
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise McpBrokerError("MCP schema description is too long")
        normalized["description"] = description
    if "enum" in schema:
        if not isinstance(schema["enum"], list) or not 1 <= len(schema["enum"]) <= 64:
            raise McpBrokerError("MCP schema enum is invalid")
        normalized["enum"] = schema["enum"]
    if "default" in schema:
        default = schema["default"]
        if isinstance(default, dict | list) or (
            isinstance(default, str) and len(default) > 1_000
        ):
            raise McpBrokerError("MCP schema default is invalid")
        normalized["default"] = default
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            value = schema[key]
            if not isinstance(value, int) or value < 0 or value > 10_000:
                raise McpBrokerError(f"MCP schema {key} is invalid")
            normalized[key] = value
    for key in ("minimum", "maximum"):
        if key in schema:
            value = schema[key]
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise McpBrokerError(f"MCP schema {key} is invalid")
            normalized[key] = value
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or len(properties) > 64
            or not isinstance(required, list)
        ):
            raise McpBrokerError("MCP object schema is invalid")
        if "additionalProperties" in schema and schema.get("additionalProperties") is not False:
            raise McpBrokerError("MCP object schemas must set additionalProperties=false")
        normalized["properties"] = {
            str(name): _validate_schema_definition(value, depth=depth + 1)
            for name, value in properties.items()
            if TOOL_RE.fullmatch(str(name))
        }
        if len(normalized["properties"]) != len(properties):
            raise McpBrokerError("MCP property name is invalid")
        if any(name not in normalized["properties"] for name in required):
            raise McpBrokerError("MCP required property is not declared")
        normalized["required"] = list(dict.fromkeys(str(name) for name in required))
        normalized["additionalProperties"] = False
    elif kind == "array":
        normalized["items"] = _validate_schema_definition(schema.get("items"), depth=depth + 1)
    return normalized


def _validate_value(value: Any, schema: Mapping[str, Any], path: str = "arguments") -> None:
    if "anyOf" in schema:
        failures = 0
        for branch in schema["anyOf"]:
            try:
                _validate_value(value, branch, path)
                return
            except McpBrokerError:
                failures += 1
        if failures:
            raise McpBrokerError(f"{path} does not match an allowed schema")
    kind = schema["type"]
    valid = {
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "null": value is None,
    }[kind]
    if not valid:
        raise McpBrokerError(f"{path} has the wrong type")
    if "enum" in schema and value not in schema["enum"]:
        raise McpBrokerError(f"{path} is not an allowed value")
    if kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 4_000):
            raise McpBrokerError(f"{path} length is invalid")
    elif kind in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise McpBrokerError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise McpBrokerError(f"{path} exceeds maximum")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 64):
            raise McpBrokerError(f"{path} item count is invalid")
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], f"{path}[{index}]")
    elif kind == "object":
        properties = schema["properties"]
        extras = set(value) - set(properties)
        missing = set(schema.get("required", [])) - set(value)
        if extras or missing:
            raise McpBrokerError(f"{path} has unknown or missing fields")
        for name, item in value.items():
            _validate_value(item, properties[name], f"{path}.{name}")


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        return "<truncated>"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 64:
                output["truncated"] = True
                break
            name = str(key)[:128]
            output[name] = (
                "<redacted>"
                if SECRET_KEY_RE.search(name)
                else _sanitize(item, depth=depth + 1)
            )
        return output
    if isinstance(value, list):
        return [_sanitize(item, depth=depth + 1) for item in value[:64]]
    if isinstance(value, str):
        text = EMAIL_RE.sub("<redacted-email>", value)
        text = PHONE_RE.sub("<redacted-phone>", text)
        return text[:4_000]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:1_000]


class StdioMcpClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._request_id = 0
        self._write_lock = asyncio.Lock()

    async def start(self) -> list[dict[str, Any]]:
        if self.process is not None:
            raise McpBrokerError(f"MCP server {self.config.label} is already started")
        env = {
            name: os.environ[name]
            for name in SAFE_ENV_NAMES | set(self.config.environment)
            if name in os.environ
        }
        self.process = await asyncio.create_subprocess_exec(
            *self.config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=MAX_LINE_BYTES,
        )
        self._reader = asyncio.create_task(
            self._read_loop(), name=f"mcp-{self.config.label}-stdout"
        )
        self._stderr = asyncio.create_task(
            self._stderr_loop(), name=f"mcp-{self.config.label}-stderr"
        )
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "phone-agent-gateway", "version": PACKAGE_VERSION},
            },
        )
        if not isinstance(result.get("serverInfo"), dict):
            raise McpBrokerError(f"MCP server {self.config.label} returned no serverInfo")
        await self.notify("notifications/initialized", {})
        listed = await self.request("tools/list", {})
        tools = listed.get("tools")
        if not isinstance(tools, list) or len(tools) > MAX_TOOLS_PER_SERVER:
            raise McpBrokerError(f"MCP server {self.config.label} returned an invalid tool list")
        return tools

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            response = await asyncio.wait_for(future, timeout=self.config.timeout_secs)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise McpBrokerError(f"MCP {self.config.label}.{method} failed")
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpBrokerError(f"MCP {self.config.label}.{method} returned an invalid result")
        return result

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        safe = _sanitize(result)
        encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), default=str).encode()
        if len(encoded) > self.config.max_output_bytes:
            raise McpBrokerError(f"MCP tool {self.config.label}.{name} output exceeded its bound")
        return safe

    async def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise McpBrokerError(f"MCP server {self.config.label} is not running")
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        if len(encoded) > MAX_LINE_BYTES:
            raise McpBrokerError("MCP request exceeds the transport bound")
        async with self._write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                if len(line) > MAX_LINE_BYTES:
                    raise McpBrokerError("MCP response exceeds the transport bound")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id") if isinstance(message, dict) else None
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(message)
        except Exception as exc:
            self._fail_pending(exc)
        finally:
            self._fail_pending(McpBrokerError(f"MCP server {self.config.label} disconnected"))

    async def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        # Drain to prevent child deadlock, but never forward potentially private content.
        while await self.process.stderr.readline():
            pass

    def _fail_pending(self, problem: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(problem)

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader, self._stderr) if task is not None),
            return_exceptions=True,
        )
        self._reader = None
        self._stderr = None
        self._fail_pending(McpBrokerError("MCP client closed"))
        self._pending.clear()


@dataclass(frozen=True, slots=True)
class _MappedTool:
    exposed_name: str
    source_name: str
    schema: dict[str, Any]
    read_only: bool
    client: StdioMcpClient
    config: McpServerConfig


class McpToolBroker:
    def __init__(
        self,
        configs: tuple[McpServerConfig, ...],
        *,
        task_allowed_tools: set[str],
        call_id: str,
    ) -> None:
        self.configs = configs
        self.task_allowed_tools = task_allowed_tools
        self.call_id_hash = hashlib.sha256(str(call_id).encode()).hexdigest()[:16]
        self.clients: list[StdioMcpClient] = []
        self.tools: dict[str, _MappedTool] = {}

    @classmethod
    def from_environment(
        cls,
        *,
        task_allowed_tools: set[str],
        call_id: str,
    ) -> McpToolBroker:
        return cls(load_mcp_config(), task_allowed_tools=task_allowed_tools, call_id=call_id)

    async def start(self) -> dict[str, RealtimeTool]:
        mapped: dict[str, RealtimeTool] = {}
        try:
            for config in self.configs:
                client = StdioMcpClient(config)
                self.clients.append(client)
                discovered = await client.start()
                for raw in discovered:
                    tool = self._map_tool(config, client, raw)
                    if tool is None:
                        continue
                    if tool.exposed_name in mapped:
                        raise McpBrokerError(f"duplicate exposed MCP tool {tool.exposed_name}")
                    self.tools[tool.exposed_name] = tool

                    async def handler(
                        _tool: _MappedTool = tool, **arguments: Any
                    ) -> dict[str, Any]:
                        _validate_value(arguments, _tool.schema)
                        if not _tool.read_only:
                            return {
                                "completed": False,
                                "reason": "operator_approval_required",
                                "approval_reference": self.call_id_hash,
                                "say": (
                                    "Tell the caller the action needs operator approval and "
                                    "is not completed."
                                ),
                            }
                        return await _tool.client.call_tool(_tool.source_name, arguments)

                    description = str(raw["description"])[:MAX_DESCRIPTION_CHARS]
                    spec = ToolSpec(
                        name=tool.exposed_name,
                        description=description,
                        handler=handler,
                        params=tool.schema["properties"],
                        required=tuple(tool.schema.get("required", [])),
                        timeout_secs=config.timeout_secs,
                    )
                    mapped[tool.exposed_name] = RealtimeTool(
                        name=tool.exposed_name,
                        definition=spec.definition,
                        handler=None,  # type: ignore[arg-type]
                        spec=spec,
                        timeout_secs=config.timeout_secs,
                    )
            if mapped:
                logger.info("Local MCP tools ready count=%d", len(mapped))
            return mapped
        except Exception:
            await self.close()
            raise

    def _map_tool(
        self,
        config: McpServerConfig,
        client: StdioMcpClient,
        raw: Any,
    ) -> _MappedTool | None:
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "")
        if name not in config.allowed_tools or not TOOL_RE.fullmatch(name):
            return None
        exposed = f"mcp_{config.label}__{name.replace('.', '_').replace('-', '_')}"
        # The task contract remains the final authority. It names the exposed,
        # namespaced tool so two servers can never collide by accident.
        if exposed not in self.task_allowed_tools:
            return None
        description = raw.get("description")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > MAX_DESCRIPTION_CHARS
        ):
            raise McpBrokerError(f"MCP tool {config.label}.{name} has an invalid description")
        schema = _validate_schema_definition(raw.get("inputSchema"))
        if schema.get("type") != "object":
            raise McpBrokerError(f"MCP tool {config.label}.{name} input must be an object")
        annotations = raw.get("annotations") or {}
        if not isinstance(annotations, dict):
            raise McpBrokerError(f"MCP tool {config.label}.{name} annotations are invalid")
        read_only = annotations.get("readOnlyHint") is True
        return _MappedTool(exposed, name, schema, read_only, client, config)

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self.clients), return_exceptions=True)
        self.clients.clear()
        self.tools.clear()
