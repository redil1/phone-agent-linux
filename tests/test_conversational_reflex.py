"""Tests for context-safe cached conversational reactions."""

from __future__ import annotations

from typing import Any

import pytest
from phone_agent_gateway.ai_bridge.conversational_reflex import (
    ConversationalReflexProcessor,
    select_reflex,
)
from pipecat.frames.frames import (
    LLMContextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection


class _CachedReflexTTS:
    def __init__(self, pcm: bytes | None = b"\x01\x00" * 1600) -> None:
        self.pcm = pcm
        self.warmed: list[tuple[str, ...]] = []
        self.substantive_ready = False

    async def warm_reflexes(self, phrases: tuple[str, ...]) -> None:
        self.warmed.append(phrases)

    def get_reflex_pcm(self, _phrase: str) -> bytes | None:
        return self.pcm

    def has_ready_speculative_audio(self) -> bool:
        return self.substantive_ready


def test_reflex_selection_suppresses_trivial_turns_and_detects_french() -> None:
    assert select_reflex("Okay.", "en-US") is None
    assert select_reflex("Why did you call me today?", "en-US") == "I see."
    assert select_reflex("Pourquoi vous m'avez appelé aujourd'hui ?", "en-US") == "Je vois."


@pytest.mark.asyncio
async def test_reflex_audio_precedes_llm_without_mutating_context() -> None:
    tts = _CachedReflexTTS()
    processor = ConversationalReflexProcessor(
        tts=tts,
        language="en-US",
        enabled=True,
        cooldown_ms=0,
    )
    context = LLMContext(
        messages=[
            {"role": "system", "content": "Be accurate."},
            {"role": "user", "content": "Why did you call me today?"},
        ]
    )
    original_messages = [dict(message) for message in context.get_messages()]
    original = LLMContextFrame(context=context)
    pushed: list[Any] = []

    async def capture(
        frame: Any,
        _direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        pushed.append(frame)

    processor.push_frame = capture  # type: ignore[method-assign]
    await processor.process_frame(original, FrameDirection.DOWNSTREAM)

    assert isinstance(pushed[0], TTSStartedFrame)
    assert pushed[0].append_to_context is False
    assert any(isinstance(frame, TTSAudioRawFrame) for frame in pushed)
    assert isinstance(pushed[-2], TTSStoppedFrame)
    assert pushed[-1] is original
    assert context.get_messages() == original_messages


@pytest.mark.asyncio
async def test_reflex_never_waits_when_cache_is_missing_or_answer_audio_is_ready() -> None:
    for pcm, substantive_ready in ((None, False), (b"\x01\x00" * 1600, True)):
        tts = _CachedReflexTTS(pcm)
        tts.substantive_ready = substantive_ready
        processor = ConversationalReflexProcessor(
            tts=tts,
            language="en-US",
            enabled=True,
            cooldown_ms=0,
        )
        context_frame = LLMContextFrame(
            context=LLMContext(messages=[{"role": "user", "content": "Please explain this issue."}])
        )
        pushed: list[Any] = []

        async def capture(
            frame: Any,
            _direction=FrameDirection.DOWNSTREAM,
            captured=pushed,
        ) -> None:
            captured.append(frame)

        processor.push_frame = capture  # type: ignore[method-assign]
        await processor.process_frame(context_frame, FrameDirection.DOWNSTREAM)

        assert pushed == [context_frame]
        await processor.cleanup()
