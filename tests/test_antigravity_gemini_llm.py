"""Unit tests for Antigravity Gemini LLM Service and Ollama fallback."""

from __future__ import annotations

from typing import Any

import pytest
from phone_agent_gateway.ai_bridge.antigravity_gemini_llm import (
    AntigravityGeminiLLMService,
    _format_context_prompt,
)
from pipecat.frames.frames import (
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection


def test_format_context_prompt() -> None:
    context = LLMContext()
    context.add_message({"role": "system", "content": "You are a concise voice bot."})
    context.add_message({"role": "user", "content": "Hello there."})
    context.add_message({"role": "assistant", "content": "Hi! How can I help?"})
    context.add_message({"role": "user", "content": "What is telephony?"})

    prompt = _format_context_prompt(context, system_instruction="Stay concise.")
    assert "System instructions:" in prompt
    assert "User: Hello there." in prompt
    assert "Assistant: Hi! How can I help?" in prompt
    assert "User: What is telephony?" in prompt


@pytest.mark.asyncio
async def test_antigravity_gemini_service_generation_flow() -> None:
    service = AntigravityGeminiLLMService(
        model="gemini-2.5-flash",
        system_instruction="Be concise.",
    )

    # Mock discover and generate
    service._base_url = "https://127.0.0.1:53857"
    service._csrf_token = "test-csrf"

    async def mock_generate(prompt: str) -> str:
        return "Telephony is distance voice transmission."

    service._generate_gemini = mock_generate  # type: ignore[method-assign]

    pushed_frames: list[Any] = []

    async def capture_push(
        frame: Any, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        pushed_frames.append(frame)

    service.push_frame = capture_push  # type: ignore[method-assign]

    context = LLMContext()
    context.add_message({"role": "user", "content": "Explain telephony."})

    await service.process_frame(LLMContextFrame(context=context), FrameDirection.DOWNSTREAM)

    types = [type(f) for f in pushed_frames]
    assert LLMFullResponseStartFrame in types
    assert LLMTextFrame in types
    assert LLMFullResponseEndFrame in types

    text_frames = [f.text for f in pushed_frames if isinstance(f, LLMTextFrame)]
    full_text = "".join(text_frames)
    assert full_text == "Telephony is distance voice transmission."


@pytest.mark.asyncio
async def test_exact_normalized_final_prompt_reuses_speculative_gemini() -> None:
    service = AntigravityGeminiLLMService(model="gemini-3.1-flash-lite")
    service._session = object()  # type: ignore[assignment]
    calls = 0

    async def mock_generate(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "Of course, I can help with that."

    service._generate_gemini = mock_generate  # type: ignore[method-assign]
    interim = LLMContext(messages=[{"role": "user", "content": "Can you help me"}])
    task = service.start_prefetch(interim)
    assert task is not None
    await task

    pushed_frames: list[Any] = []

    async def capture_push(frame: Any, _direction=FrameDirection.DOWNSTREAM) -> None:
        pushed_frames.append(frame)

    service.push_frame = capture_push  # type: ignore[method-assign]
    final = LLMContext(messages=[{"role": "user", "content": "Can you help me?"}])
    await service.process_frame(LLMContextFrame(context=final), FrameDirection.DOWNSTREAM)

    assert calls == 1
    assert service._prefetch_hits == 1
    assert (
        "".join(frame.text for frame in pushed_frames if isinstance(frame, LLMTextFrame))
        == "Of course, I can help with that."
    )
