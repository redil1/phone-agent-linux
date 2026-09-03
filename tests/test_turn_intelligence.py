"""Tests for Turn Intelligence, Duplex Audio, and STT (Milestone 6)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.turn_intelligence import UnifiedTurnController


def test_turn_controller_assembles_fragments_and_increments_epoch() -> None:
    controller = UnifiedTurnController()
    assert controller.current_epoch == 0

    # Intermediate fragment
    res1 = controller.process_fragment("I would like", is_final=False, confidence=0.9)
    assert res1 is None

    # Final fragment
    res2 = controller.process_fragment("to schedule a visit.", is_final=True, confidence=0.95)
    assert res2 is not None
    assert res2.transcript == "I would like to schedule a visit."
    assert res2.is_final is True
    assert res2.epoch == 0
    assert controller.current_epoch == 1


def test_echo_rejection_blocks_agent_downlink() -> None:
    controller = UnifiedTurnController()
    controller.register_agent_speech("Thank you for calling Acme Corp. How can I help?")

    # Exact echo from speakerphone feedback suppressed
    res = controller.process_fragment("Thank you for calling Acme Corp.", is_final=True, confidence=0.99)
    assert res is None
