"""Tests for LLM Routing, Context Bounds, and Quality (Milestone 7)."""

from __future__ import annotations

import pytest

from phone_agent_gateway.ai_bridge.model_router import (
    ModelRegistry,
    StableContextLayout,
)


def test_model_registry_lookup() -> None:
    reg = ModelRegistry()
    prof = reg.get_profile("gemini-flash")
    assert prof.tier == "cloud_premium"
    assert prof.streaming_supported is True

    with pytest.raises(KeyError):
        reg.get_profile("nonexistent")


def test_stable_context_layout_bounds_dialogue_tail() -> None:
    ctx = StableContextLayout(
        immutable_system_prefix="Identity: Acme Sales Rep",
        verified_knowledge=["Product price is $50/mo"],
        max_recent_turns=4,
    )
    for i in range(6):
        ctx.add_turn("user", f"Question {i}")
        ctx.add_turn("assistant", f"Answer {i}")

    # Maximum 4 turns retained in bounded tail
    assert len(ctx.recent_dialogue_turns) == 4
    rendered = ctx.render_prompt_bundle()
    assert rendered[0]["role"] == "system"
    assert "Verified Knowledge" in rendered[1]["content"]
    assert rendered[-1]["content"] == "Answer 5"
