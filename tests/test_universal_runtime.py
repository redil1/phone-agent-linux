"""Tests for Universal Low-Latency Agent Runtime (Milestone 5)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.universal_runtime import (
    SemanticNoveltyTracker,
    TurnInput,
    plan_turn_response,
)


def test_turn_input_and_structured_output() -> None:
    turn = TurnInput(text="What are your hours?", confidence=0.98)
    assert turn.language == "en-US"
    assert turn.channel == "gsm"

    output = plan_turn_response(turn)
    assert output.intent == "answer_first_progress"
    assert "What are your hours?" in output.spoken_text
    assert output.completion_decision == "continue"


def test_semantic_novelty_tracker_prevents_loops() -> None:
    tracker = SemanticNoveltyTracker()
    assert tracker.check_and_record("Hello, how can I help you today?") is True
    # Immediate repeat blocked
    assert tracker.check_and_record("Hello, how can I help you today?") is False
    # Distinct sentence allowed
    assert tracker.check_and_record("What service are you looking for?") is True
