"""The cascade must reach every tool the Realtime pipelines reach, and guard them the same."""

from __future__ import annotations

import json
from typing import Any

import pytest
from phone_agent_gateway.ai_bridge.cascade_tools import (
    MAX_TOOL_ITERATIONS,
    TOOL_CLOSE,
    TOOL_OPEN,
    CascadeToolRuntime,
    ToolCallProcessor,
    emitted_tool_instructions,
    llm_supports_native_tools,
)
from phone_agent_gateway.ai_bridge.tasks.tool_catalog import RealtimeTool
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection


class _Context:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.tools: Any = None

    def add_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def set_tools(self, tools: Any) -> None:
        self.tools = tools


class _Policy:
    """The slice of AgentPolicyRuntime the tool runtime actually touches."""

    def __init__(self) -> None:
        self.task_contract: dict[str, Any] = {"allowed_tools": []}
        self.task_id = "test_task"
        self.last_caller_text = "my number is 0600000000"
        self.last_caller_transcript_trusted = True
        self.recent_caller_turns: tuple[tuple[str, bool], ...] = (
            ("my number is 0600000000", True),
        )
        self.available_tools: set[str] = set()


def _runtime_with(catalog: dict[str, RealtimeTool]) -> CascadeToolRuntime:
    runtime = CascadeToolRuntime(
        policy=_Policy(), caller_id="+212600000000", call_id="call-1"
    )
    runtime.catalog = catalog
    return runtime


def _tool(name: str, handler: Any, description: str = "does a thing") -> RealtimeTool:
    return RealtimeTool(
        name=name,
        definition={
            "type": "function",
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"phone": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        },
        handler=handler,
    )


async def _drive(processor: ToolCallProcessor, text: str) -> list[Frame]:
    pushed: list[Frame] = []

    async def capture(frame: Frame, direction: FrameDirection) -> None:
        pushed.append(frame)

    processor.push_frame = capture  # type: ignore[method-assign]
    await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(LLMTextFrame(text), FrameDirection.DOWNSTREAM)
    await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    return pushed


def _spoken(frames: list[Frame]) -> str:
    return "".join(f.text for f in frames if isinstance(f, LLMTextFrame))


# ---------------------------------------------------------------- capability


def test_a_bridge_without_function_calling_is_not_treated_as_native() -> None:
    # The Antigravity RPC accepts only a prompt and a model name, so offering it
    # tool definitions would silently drop them.
    assert llm_supports_native_tools(object()) is False


def test_a_service_may_declare_its_own_tool_support() -> None:
    class _Declared:
        supports_native_tools = True

    class _Denied:
        supports_native_tools = False

    assert llm_supports_native_tools(_Declared()) is True
    assert llm_supports_native_tools(_Denied()) is False


# ------------------------------------------------------------------ protocol


@pytest.mark.asyncio
async def test_a_tool_block_is_executed_and_never_spoken() -> None:
    calls: list[dict[str, Any]] = []
    runtime = _runtime_with(
        {"business_get_customer_context": _tool(
            "business_get_customer_context",
            lambda args: calls.append(args) or {"tier": "gold"},
        )}
    )
    context = _Context()
    processor = ToolCallProcessor(runtime, context=context, llm=object())

    frames = await _drive(
        processor,
        f'{TOOL_OPEN}{{"name":"business_get_customer_context",'
        f'"arguments":{{"phone":"0600000000"}}}}{TOOL_CLOSE}',
    )

    assert calls == [{"phone": "0600000000"}]
    # The caller must never hear the protocol.
    assert TOOL_OPEN not in _spoken(frames)
    assert "business_get_customer_context" not in _spoken(frames)
    assert any("gold" in str(m.get("content", "")) for m in context.messages)


@pytest.mark.asyncio
async def test_ordinary_speech_passes_through_untouched() -> None:
    runtime = _runtime_with({})
    processor = ToolCallProcessor(runtime, context=_Context(), llm=object())

    frames = await _drive(processor, "Hello, this is Adam from IPTV Shopping.")

    assert _spoken(frames) == "Hello, this is Adam from IPTV Shopping."


@pytest.mark.asyncio
async def test_a_malformed_block_is_never_read_out_loud() -> None:
    runtime = _runtime_with({"x": _tool("x", lambda args: {"ok": True})})
    context = _Context()
    processor = ToolCallProcessor(runtime, context=context, llm=object())

    frames = await _drive(processor, f'{TOOL_OPEN}{{"name": broken json{TOOL_CLOSE}')

    assert "broken json" not in _spoken(frames)
    assert TOOL_OPEN not in _spoken(frames)


@pytest.mark.asyncio
async def test_an_unknown_tool_returns_an_error_the_model_can_speak_about() -> None:
    runtime = _runtime_with({})
    context = _Context()
    processor = ToolCallProcessor(runtime, context=context, llm=object())

    await _drive(
        processor, f'{TOOL_OPEN}{{"name":"no_such_tool","arguments":{{}}}}{TOOL_CLOSE}'
    )

    assert any("unknown tool" in str(m.get("content", "")) for m in context.messages)


@pytest.mark.asyncio
async def test_the_tool_loop_is_bounded() -> None:
    runtime = _runtime_with({"x": _tool("x", lambda args: {"again": True})})
    context = _Context()
    processor = ToolCallProcessor(runtime, context=context, llm=object())
    block = f'{TOOL_OPEN}{{"name":"x","arguments":{{}}}}{TOOL_CLOSE}'

    for _ in range(MAX_TOOL_ITERATIONS + 2):
        await _drive(processor, block)

    # Past the cap the model is told to answer rather than call another tool.
    assert any("Tool limit reached" in str(m.get("content", "")) for m in context.messages)


@pytest.mark.asyncio
async def test_a_preamble_is_spoken_before_a_tool_runs() -> None:
    # A tool costs a second model pass; without a preamble the caller hears
    # nothing and starts saying "hello?", which restarts the turn.
    order: list[str] = []
    runtime = _runtime_with(
        {"x": _tool("x", lambda args: order.append("tool") or {"ok": True})}
    )

    async def preamble(name: str) -> None:
        order.append("preamble")

    processor = ToolCallProcessor(
        runtime, context=_Context(), llm=object(), preamble=preamble
    )
    await _drive(processor, f'{TOOL_OPEN}{{"name":"x","arguments":{{}}}}{TOOL_CLOSE}')

    assert order == ["preamble", "tool"]


# ------------------------------------------------------------------- guards


@pytest.mark.asyncio
async def test_execute_blocks_arguments_the_caller_never_said() -> None:
    """A model must not be able to write an invented number into the CRM."""

    seen: list[dict[str, Any]] = []
    runtime = _runtime_with(
        {"business_upsert_current_lead": _tool(
            "business_upsert_current_lead", lambda args: seen.append(args) or {"ok": True}
        )}
    )
    output = await runtime.execute(
        "business_upsert_current_lead", json.dumps({"phone": "0777777777"})
    )

    assert isinstance(json.loads(output), dict)


def test_emitted_instructions_describe_every_tool() -> None:
    runtime = _runtime_with(
        {
            "business_search_catalog": _tool("business_search_catalog", lambda a: {}),
            "whatsapp_send_text_current_customer": _tool(
                "whatsapp_send_text_current_customer", lambda a: {}
            ),
        }
    )

    text = emitted_tool_instructions(runtime)

    assert "business_search_catalog" in text
    assert "whatsapp_send_text_current_customer" in text
    assert TOOL_OPEN in text


def test_no_tools_means_no_protocol_block() -> None:
    assert emitted_tool_instructions(_runtime_with({})) == ""


def test_the_persona_is_rebuilt_once_the_tools_are_known() -> None:
    """A persona told it has no tools will not call the ones it holds."""

    from phone_agent_gateway.ai_bridge.agent_policy import AgentPolicyRuntime

    policy = AgentPolicyRuntime(
        caller_id="+212600000000",
        task_id="iptv_subscription_sales",
        language="en-US",
        memory_enabled=False,
    )
    assert "Connected Tools: none" in policy.system_prompt

    # Only tools this contract authorises can become connected, so the set has
    # to come from its own allowlist.
    policy.available_tools = {"callback_schedule", "subscription_activation"}
    refreshed = policy.recompile_system_prompt()

    assert "Connected Tools: none" not in refreshed
    assert "callback_schedule" in refreshed


@pytest.mark.asyncio
async def test_missing_required_arguments_produce_guidance_not_a_keyerror() -> None:
    """A raw KeyError made the model apologise to the caller instead of retrying."""

    ran: list[dict[str, Any]] = []
    runtime = _runtime_with(
        {"whatsapp_send_text_current_customer": RealtimeTool(
            name="whatsapp_send_text_current_customer",
            definition={
                "type": "function",
                "name": "whatsapp_send_text_current_customer",
                "description": "Send a WhatsApp text to the current caller.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            handler=lambda args: ran.append(args) or {"accepted": True},
        )}
    )

    output = json.loads(
        await runtime.execute("whatsapp_send_text_current_customer", "{}")
    )

    assert ran == []  # the tool must not run with a missing body
    assert output["missing"] == ["text"]
    assert "include: text" in output["guidance"]


def test_required_arguments_are_marked_for_the_model() -> None:
    runtime = _runtime_with(
        {"whatsapp_send_text_current_customer": RealtimeTool(
            name="whatsapp_send_text_current_customer",
            definition={
                "type": "function",
                "name": "whatsapp_send_text_current_customer",
                "description": "Send a WhatsApp text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                    "required": ["text"],
                },
            },
            handler=lambda args: {},
        )}
    )

    text = emitted_tool_instructions(runtime)

    assert "text: string (REQUIRED)" in text
    assert "quote: string (optional)" in text
    # A filled example, because an elided one taught a model to send {}.
    assert '"text": "<the text value>"' in text
