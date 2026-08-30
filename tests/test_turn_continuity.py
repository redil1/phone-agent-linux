"""Regression tests for semantic fragments and call-stage continuity."""

from __future__ import annotations

from typing import Any

import pytest
from phone_agent_gateway.ai_bridge.turn_continuity import (
    SemanticTurnGuardProcessor,
    is_semantically_incomplete_fragment,
    looks_semantically_incomplete,
)
from pipecat.frames.frames import TranscriptionFrame, UserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection


@pytest.mark.parametrize("text", ["ديال", "because", "et", "de"])
def test_high_confidence_connectors_are_incomplete(text: str) -> None:
    assert is_semantically_incomplete_fragment(text)


@pytest.mark.parametrize(
    "text",
    ["Oui", "Non", "sport", "football", "Yes", "No", "Oui, vas-y."],
)
def test_meaningful_short_answers_are_not_suppressed(text: str) -> None:
    assert not is_semantically_incomplete_fragment(text)


def test_endpointing_recognizes_trailing_fragments_without_suppressing_real_questions() -> None:
    assert looks_semantically_incomplete("I need help because")
    assert looks_semantically_incomplete("Yes, please go")
    assert not is_semantically_incomplete_fragment("why")


@pytest.mark.parametrize("text", ["Hello,", "Bonjour,", "So the thing is:", "I mean -"])
def test_clauses_left_on_continuation_punctuation_wait_longer(text: str) -> None:
    # A comma is the caller drawing breath. Endpointing on it committed
    # "Hello," as a whole turn and the agent answered a greeting fragment.
    assert looks_semantically_incomplete(text)


@pytest.mark.parametrize("text", ["Yes.", "Oui.", "That works!", "Who is this?"])
def test_terminated_sentences_still_endpoint_immediately(text: str) -> None:
    assert not looks_semantically_incomplete(text)


@pytest.mark.asyncio
async def test_incomplete_fragment_cannot_trigger_a_model_turn() -> None:
    processor = SemanticTurnGuardProcessor()
    pushed: list[Any] = []

    async def capture(frame: Any, direction: FrameDirection) -> None:
        pushed.append((frame, direction))

    processor.push_frame = capture  # type: ignore[method-assign]
    await processor.process_frame(
        TranscriptionFrame(text="ديال", user_id="caller", timestamp=None),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert not any(isinstance(frame, TranscriptionFrame) for frame, _direction in pushed)
    assert any(isinstance(frame, UserStoppedSpeakingFrame) for frame, _direction in pushed)


@pytest.mark.asyncio
async def test_complete_follow_up_passes_unchanged_after_a_fragment() -> None:
    processor = SemanticTurnGuardProcessor()
    pushed: list[Any] = []

    async def capture(frame: Any, direction: FrameDirection) -> None:
        pushed.append((frame, direction))

    processor.push_frame = capture  # type: ignore[method-assign]
    complete = TranscriptionFrame(
        text="Je regarde surtout les matchs de football.",
        user_id="caller",
        timestamp=None,
    )
    await processor.process_frame(complete, FrameDirection.DOWNSTREAM)

    assert pushed == [(complete, FrameDirection.DOWNSTREAM)]
