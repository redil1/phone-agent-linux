"""Full tool, MCP and business-suite access for the Standard Cascade.

The cascade previously reached the model with no tools at all: it never built a
catalog, never started the CRM/ERP, WhatsApp, web-research or MCP runtimes, and
handed Pipecat a bare ``LLMContext``. An agent on a task whose contract allowed
nineteen tools could therefore offer a customer a WhatsApp message it had no
mechanism to send.

This module gives the cascade the same surface the Realtime pipelines have,
without touching them. It owns one call's catalog, keeps it hot-reloaded, and
exposes a single guarded ``execute`` that applies argument grounding before any
tool runs.

Two adapters carry a tool call to the model, because the cascade's LLMs do not
agree on how one is made:

* ``NativeToolBinding`` for models with real function calling (Ollama, OpenAI,
  OpenRouter, LM Studio, Gemini). Pipecat owns the loop.
* ``ToolCallProcessor`` for models with none, most importantly the Antigravity
  bridge, whose RPC accepts only a prompt and a model name. The model emits a
  delimited block, this processor executes it and feeds the result back.

Both paths run the identical catalog and the identical guards, so a tool behaves
the same however it was requested.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .frappe_integration import FrappeConfigStore, FrappeToolRuntime
from .mcp_broker import McpToolBroker
from .openwa_integration import OpenWAConfigStore, OpenWAToolRuntime
from .tasks.tool_catalog import (
    END_CALL_TOOL_NAME,
    RealtimeTool,
    build_end_call_tool,
    build_tool_catalog,
    execute_tool,
    tool_definitions,
)
from .tool_argument_grounding import ground_tool_arguments
from .tool_control import ManagedToolRuntime, ToolControlStore
from .web_research import WebResearchConfigStore, WebResearchToolRuntime

logger = logging.getLogger("PhoneAgentCascadeTools")

EventSink = Callable[[dict[str, Any]], Awaitable[None] | None]

# The emitted protocol's delimiters. They are deliberately not markdown fences:
# a model that writes code examples uses those, and one confusable token would
# be spoken at a caller.
TOOL_OPEN = "<tool_call>"
TOOL_CLOSE = "</tool_call>"
_TOOL_BLOCK = re.compile(
    re.escape(TOOL_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(TOOL_CLOSE), re.DOTALL
)
# Bound the tool loop. Without this a model that keeps requesting tools holds the
# caller in silence for as long as it likes.
MAX_TOOL_ITERATIONS = 3


def llm_supports_native_tools(llm: Any) -> bool:
    """Whether this service can carry function definitions to its model.

    Checked by capability rather than class name so a provider added later is
    picked up without editing this module. The local bridges are excluded by
    the same test they fail in practice: they declare no tool support at all.
    """

    if getattr(llm, "supports_native_tools", None) is True:
        return True
    if getattr(llm, "supports_native_tools", None) is False:
        return False
    return callable(getattr(llm, "register_function", None)) and bool(
        getattr(llm, "_supports_tools", False)
    )


class CascadeToolRuntime:
    """One call's complete tool surface, hot-reloadable while it is live."""

    def __init__(
        self,
        *,
        policy: Any,
        caller_id: str,
        call_id: str,
        system_prompt: str = "",
        event_sink: EventSink | None = None,
    ) -> None:
        self.policy = policy
        self.caller_id = caller_id
        self.call_id = call_id
        self.system_prompt = system_prompt
        self._event_sink = event_sink
        self.catalog: dict[str, RealtimeTool] = {}

        self._contract_allowed_tools: set[str] = set()
        self._stores = {
            "managed": ToolControlStore(),
            "openwa": OpenWAConfigStore(),
            "web_research": WebResearchConfigStore(),
            "frappe": FrappeConfigStore(),
        }
        self._runtimes: dict[str, Any] = dict.fromkeys(self._stores)
        self._tool_names: dict[str, set[str]] = {key: set() for key in self._stores}
        self._fingerprints: dict[str, str] = dict.fromkeys(self._stores, "")
        self._retired: list[Any] = []
        self._mcp_broker: McpToolBroker | None = None
        self._reload_lock = asyncio.Lock()
        self._watcher: asyncio.Task[None] | None = None
        self._running = False

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> dict[str, RealtimeTool]:
        """Build the contract catalog, then attach every configured runtime."""

        self.catalog = build_tool_catalog(self.policy.task_contract, self.policy.task)
        # Ending the call is a conversational control the model should own
        # rather than a phrase matcher guessing from the transcript.
        self.catalog[END_CALL_TOOL_NAME] = build_end_call_tool()
        contract = self.policy.task_contract
        contract["allowed_tools"] = sorted(
            {str(name) for name in contract.get("allowed_tools", []) or []} | {END_CALL_TOOL_NAME}
        )
        self._contract_allowed_tools = set(contract["allowed_tools"])

        skill_tool = self.policy.persona_compiler.identity_kernel.realtime_skill_tool(
            task_id=self.policy.task_id,
            language=getattr(self.policy, "language", "en-US"),
            authorized_tools=set(self._contract_allowed_tools),
        )
        if skill_tool is not None:
            self.catalog[skill_tool.name] = skill_tool

        self._running = True
        for key in ("managed", "openwa", "web_research", "frappe"):
            # One unreachable backend must not deny the call every other tool.
            with contextlib.suppress(Exception):
                await self._reload(key)
        await self._start_mcp()
        self._refresh_permissions()
        self._watcher = asyncio.create_task(self._watch(), name="cascade-tool-watcher")
        logger.info(
            "cascade tools ready count=%d tools=%s",
            len(self.catalog),
            ",".join(sorted(self.catalog)),
        )
        return self.catalog

    async def _start_mcp(self) -> None:
        try:
            self._mcp_broker = McpToolBroker.from_environment(
                task_allowed_tools=set(self._contract_allowed_tools),
                call_id=self.call_id,
            )
            tools = await self._mcp_broker.start()
        except Exception:
            logger.warning("MCP broker unavailable for this call", exc_info=True)
            return
        collisions = set(self.catalog) & set(tools)
        if collisions:
            logger.error("ignoring MCP tools that collide: %s", ",".join(sorted(collisions)))
            tools = {n: t for n, t in tools.items() if n not in collisions}
        self.catalog.update(tools)

    async def close(self) -> None:
        self._running = False
        watcher = self._watcher
        self._watcher = None
        if watcher is not None and not watcher.done():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        for runtime in list(self._runtimes.values()) + self._retired:
            if runtime is not None:
                with contextlib.suppress(Exception):
                    await runtime.close()
        self._retired.clear()
        if self._mcp_broker is not None:
            with contextlib.suppress(Exception):
                await self._mcp_broker.close()

    # ------------------------------------------------------------- hot reload

    def _make_runtime(self, key: str, config: Any) -> Any:
        if key == "managed":
            return ManagedToolRuntime(
                config,
                task_id=self.policy.task_id,
                call_id=self.call_id,
                event_sink=self._emit,
            )
        if key == "openwa":
            return OpenWAToolRuntime(
                config,
                caller_id=self.caller_id,
                task_id=self.policy.task_id,
                call_id=self.call_id,
                event_sink=self._emit,
            )
        if key == "web_research":
            return WebResearchToolRuntime(
                config,
                task_id=self.policy.task_id,
                event_sink=self._emit,
            )
        return FrappeToolRuntime(
            config,
            caller_id=self.caller_id,
            task_id=self.policy.task_id,
            call_id=self.call_id,
            call_direction=self.policy.call_context.direction.value,
            event_sink=self._emit,
        )

    async def _reload(self, key: str) -> None:
        """Swap one runtime's tools in, keeping the rest of the catalog intact."""

        async with self._reload_lock:
            store = self._stores[key]
            config = await asyncio.to_thread(store.load)
            fingerprint = await asyncio.to_thread(store.fingerprint)
            candidate = self._make_runtime(key, config)
            try:
                tools = await candidate.start()
                retained = {
                    name: tool
                    for name, tool in self.catalog.items()
                    if name not in self._tool_names[key]
                }
                collisions = set(retained) & set(tools)
                if collisions:
                    raise RuntimeError(
                        f"{key} tool name collides with an existing tool: "
                        + ", ".join(sorted(collisions))
                    )
            except Exception:
                await candidate.close()
                raise
            previous = self._runtimes[key]
            self._runtimes[key] = candidate
            self._tool_names[key] = set(tools)
            self.catalog = {**retained, **tools}
            self._fingerprints[key] = fingerprint
            if previous is not None:
                self._retired.append(previous)
            self._refresh_permissions()
            await self._emit(
                {
                    "type": f"{key}_tools_reloaded",
                    "active_tools": sorted(self._tool_names[key]),
                    "pipeline": "cascade",
                }
            )

    async def _watch(self) -> None:
        """Apply operator activation changes to a call that is already running."""

        while self._running:
            await asyncio.sleep(1.0)
            for key, store in self._stores.items():
                if not self._running:
                    return
                try:
                    fingerprint = await asyncio.to_thread(store.fingerprint)
                except Exception:
                    continue
                if fingerprint == self._fingerprints[key]:
                    continue
                try:
                    await self._reload(key)
                    logger.info("reloaded %s tools mid-call", key)
                except Exception:
                    logger.warning("could not reload %s tools mid-call", key, exc_info=True)
                    self._fingerprints[key] = fingerprint

    def _refresh_permissions(self) -> None:
        names: set[str] = set(self._contract_allowed_tools)
        for tools in self._tool_names.values():
            names |= tools
        self.policy.task_contract["allowed_tools"] = sorted(names)
        self.policy.available_tools = set(self.catalog)

    # -------------------------------------------------------------- execution

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return tool_definitions(self.catalog)

    async def execute(self, name: str, raw_arguments: str) -> str:
        """Run one tool behind the same guards the Realtime path applies.

        Argument grounding matters more here than anywhere: a model that invents
        a phone number or an order id would otherwise write it into the CRM as
        though the caller had said it.
        """

        grounding = ground_tool_arguments(
            name,
            raw_arguments,
            self.policy.last_caller_text,
            transcript_trusted=getattr(self.policy, "last_caller_transcript_trusted", True),
            caller_turns=tuple(getattr(self.policy, "recent_caller_turns", ()) or ()),
        )
        if grounding.grounded_fields:
            await self._emit(
                {
                    "type": "tool_arguments_grounded",
                    "name": name,
                    "fields": list(grounding.grounded_fields),
                    "blocked": grounding.blocked,
                }
            )
        missing = self._missing_required(name, grounding.raw_arguments)
        if missing:
            # A raw KeyError told the model nothing it could act on, so it
            # apologised to the caller for a "technical hiccup" and never
            # retried. Naming the omission lets it call the tool properly.
            output = json.dumps(
                {
                    "error": "missing required arguments",
                    "missing": missing,
                    "guidance": (
                        f"Call {name} again and include: {', '.join(missing)}. "
                        "Do not tell the caller anything failed."
                    ),
                }
            )
        elif grounding.blocked:
            output = grounding.blocked_output()
        else:
            output = await execute_tool(self.catalog, name, grounding.raw_arguments)
        logger.info(
            "cascade tool call name=%s argument_chars=%d result_chars=%d",
            name,
            len(grounding.raw_arguments),
            len(output),
        )
        await self._emit(
            {
                "type": "tool_call",
                "name": name,
                "arguments": grounding.raw_arguments,
                "result": output,
                "pipeline": "cascade",
            }
        )
        return output

    def _missing_required(self, name: str, raw_arguments: str) -> list[str]:
        """Names of required arguments the model left out, checked before running."""

        tool = self.catalog.get(name)
        if tool is None:
            return []
        parameters = (tool.definition or {}).get("parameters") or {}
        required = [str(arg) for arg in parameters.get("required") or ()]
        if not required:
            return []
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return required
        if not isinstance(arguments, dict):
            return required
        return [
            arg
            for arg in required
            if arg not in arguments or arguments[arg] in (None, "")
        ]

    async def _emit(self, event: dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            result = self._event_sink(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("tool event sink failed", exc_info=True)


class NativeToolBinding:
    """Expose the catalog through the model's own function-calling protocol."""

    def __init__(self, runtime: CascadeToolRuntime, llm: Any, context: Any) -> None:
        self.runtime = runtime
        self.llm = llm
        self.context = context

    def bind(self) -> int:
        """Publish definitions and register one handler per tool.

        Returns how many tools were bound so the caller can log or fail loudly
        rather than discovering an empty toolset mid-call.
        """

        definitions = self.runtime.definitions
        if not definitions:
            return 0
        self.context.set_tools(definitions)
        for name, tool in self.runtime.catalog.items():
            self.llm.register_function(
                name,
                self._handler_for(name),
                timeout_secs=tool.timeout_secs,
            )
        return len(definitions)

    def _handler_for(self, name: str) -> Callable[[Any], Awaitable[None]]:
        async def handler(params: Any) -> None:
            arguments = params.arguments
            raw = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
            output = await self.runtime.execute(name, raw)
            try:
                result = json.loads(output)
            except json.JSONDecodeError:
                result = {"result": output}
            await params.result_callback(result)

        return handler


def _describe_arguments(definition: dict[str, Any]) -> str:
    """Render one tool's arguments so a model cannot miss the required ones.

    Listing bare names was not enough: a model called the WhatsApp tool with
    ``{}`` and the send failed on a missing ``text``. Required arguments are now
    marked and typed.
    """

    parameters = definition.get("parameters") or {}
    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or ())
    if not properties:
        return "no arguments"
    parts = []
    for arg in sorted(properties):
        spec = properties[arg] if isinstance(properties[arg], dict) else {}
        kind = str(spec.get("type", "string"))
        parts.append(f"{arg}: {kind}" + (" (REQUIRED)" if arg in required else " (optional)"))
    return ", ".join(parts)


def _example_call(runtime: CascadeToolRuntime) -> str:
    """A filled example, because an elided one taught the model to send ``{}``."""

    for name in sorted(runtime.catalog):
        definition = runtime.catalog[name].definition or {}
        parameters = definition.get("parameters") or {}
        required = list(parameters.get("required") or ())
        if required:
            example = {arg: f"<the {arg} value>" for arg in required}
            return TOOL_OPEN + json.dumps({"name": name, "arguments": example}) + TOOL_CLOSE
    first = sorted(runtime.catalog)[0]
    return TOOL_OPEN + json.dumps({"name": first, "arguments": {}}) + TOOL_CLOSE


def emitted_tool_instructions(runtime: CascadeToolRuntime) -> str:
    """The protocol block appended to the prompt of a model without tool calling."""

    if not runtime.catalog:
        return ""
    lines = [
        "# TOOL EXECUTION PROTOCOL (MANDATORY)",
        "You have live tools connected. Whenever the caller requests an action (such as sending a WhatsApp message, checking WhatsApp, searching the catalog, or scheduling a callback), you MUST execute the tool call in your response.",
        "CRITICAL: Do NOT merely reply saying you will do it in words without emitting the tool block! Always emit the tool call block.",
        "To invoke a tool, output ONLY the tool call block below (it is automatically processed by PhoneAgent and never spoken to the caller):",
        f'{TOOL_OPEN}{{"name":"<tool_name>","arguments":{{...}}}}{TOOL_CLOSE}',
        f"Example for WhatsApp: {TOOL_OPEN}{{\"name\":\"whatsapp_send_text_current_customer\",\"arguments\":{{\"text\":\"Hello, here is your requested information on WhatsApp!\"}}}}{TOOL_CLOSE}",
        "Available tools:",
    ]
    for name, tool in sorted(runtime.catalog.items()):
        definition = tool.definition or {}
        description = str(definition.get("description", "")).strip()
        lines.append(f"- {name}({_describe_arguments(definition)}): {description}")
    return "\n".join(lines)


class ToolCallProcessor(FrameProcessor):
    """Execute tool blocks from models that cannot call functions natively.

    This sits between the LLM and the response policy on purpose. Anything it
    fails to parse must never continue downstream, because the next processor
    releases sentences to speech and a caller would hear raw JSON.
    """

    def __init__(
        self,
        runtime: CascadeToolRuntime,
        *,
        context: Any,
        llm: Any,
        preamble: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.context = context
        self.llm = llm
        self._preamble = preamble
        self._buffer = ""
        self._collecting = False
        self._iterations = 0
        self._suppressed = False

    def _reset(self) -> None:
        self._buffer = ""
        self._collecting = False
        self._iterations = 0
        self._suppressed = False

    @staticmethod
    def _looks_like_tool_start(text: str) -> bool:
        """True while the text could still become a tool block.

        Holding a partial ``<tool_call>`` back is what stops the opening angle
        bracket being spoken before the rest of the block has streamed in.
        """

        stripped = text.lstrip()
        return bool(stripped) and TOOL_OPEN.startswith(stripped[: len(TOOL_OPEN)])

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction is not FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._buffer = ""
            self._collecting = True
            self._suppressed = False
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame) and self._collecting:
            self._buffer += frame.text
            if self._suppressed:
                return
            if TOOL_OPEN in self._buffer or self._looks_like_tool_start(self._buffer):
                # Withhold everything until the block resolves one way or another.
                self._suppressed = True
                return
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame) and self._collecting:
            handled = await self._maybe_run_tool(direction)
            if handled:
                return
            if self._suppressed and self._buffer.strip():
                if TOOL_OPEN in self._buffer:
                    # A tool block that never parsed. Releasing it would read the
                    # raw JSON to the caller, so it is dropped and the model is
                    # told to answer in words instead.
                    logger.warning("discarded an unparseable tool block before speech")
                    self.context.add_message(
                        {
                            "role": "system",
                            "content": "That tool call was malformed and did not run. "
                            "Answer the caller in plain words now.",
                        }
                    )
                    self._iterations += 1
                    self._buffer = ""
                    self._suppressed = False
                    if self._iterations <= MAX_TOOL_ITERATIONS:
                        await self._requeue()
                        return
                else:
                    # It was never a tool block. Release the held text so the
                    # caller hears the answer rather than silence.
                    await self.push_frame(LLMTextFrame(self._buffer), direction)
            self._collecting = False
            self._suppressed = False
            self._buffer = ""
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def _maybe_run_tool(self, direction: FrameDirection) -> bool:
        match = _TOOL_BLOCK.search(self._buffer)
        if match is None:
            return False
        if self._iterations >= MAX_TOOL_ITERATIONS:
            logger.warning("tool loop cap reached; answering without further tools")
            self.context.add_message(
                {
                    "role": "system",
                    "content": "Tool limit reached for this turn. Answer the caller now "
                    "using what you already know.",
                }
            )
            self._buffer = ""
            self._suppressed = False
            await self._requeue()
            return True

        try:
            payload = json.loads(match.group(1))
            name = str(payload.get("name", "")).strip()
            arguments = payload.get("arguments") or {}
        except (json.JSONDecodeError, AttributeError):
            logger.warning("model emitted an unparseable tool block")
            self.context.add_message(
                {
                    "role": "system",
                    "content": "That tool call was not valid JSON. Answer the caller "
                    "directly instead.",
                }
            )
            self._buffer = ""
            self._suppressed = False
            self._iterations += 1
            await self._requeue()
            return True

        if name not in self.runtime.catalog:
            output = json.dumps({"error": f"unknown tool {name}"})
        else:
            if self._preamble is not None:
                with contextlib.suppress(Exception):
                    await self._preamble(name)
            output = await self.runtime.execute(
                name, json.dumps(arguments) if not isinstance(arguments, str) else arguments
            )

        self.context.add_message({"role": "assistant", "content": match.group(0)})
        self.context.add_message(
            {"role": "system", "content": f"Result of {name}: {output}"}
        )
        self._iterations += 1
        self._buffer = ""
        self._suppressed = False
        await self._requeue()
        return True

    async def _requeue(self) -> None:
        """Ask the model to continue now that the result is in context."""

        from pipecat.frames.frames import LLMRunFrame

        await self.push_frame(LLMRunFrame(), FrameDirection.UPSTREAM)
