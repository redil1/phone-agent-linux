"""Antigravity model names must exist in the bridge's own map.

A name absent from MODEL_MAP is passed through verbatim and the bridge answers
404, several minutes into a research run.
"""

from src.extractor.subscription_providers import (
    ANTIGRAVITY_DEFAULT_MODEL,
    ANTIGRAVITY_MODELS,
)


def test_every_offered_model_is_known_to_the_bridge():
    from phone_agent_gateway.ai_bridge.antigravity_gemini_llm import MODEL_MAP

    for name in ANTIGRAVITY_MODELS:
        assert name in MODEL_MAP, f"{name} would be sent to the bridge verbatim"


def test_the_default_is_offered_and_is_a_37_flash_tier():
    assert ANTIGRAVITY_DEFAULT_MODEL in ANTIGRAVITY_MODELS
    assert ANTIGRAVITY_DEFAULT_MODEL.startswith("gemini-3.7-flash")


def test_models_the_bridge_refuses_are_not_offered():
    """Probed live: gemini-3-flash returns 404 and gemini-2.5-pro returns 503."""

    assert "gemini-3-flash" not in ANTIGRAVITY_MODELS
    assert "gemini-2.5-pro" not in ANTIGRAVITY_MODELS


def test_the_default_tier_is_listed_first():
    assert ANTIGRAVITY_MODELS[0] == ANTIGRAVITY_DEFAULT_MODEL == "gemini-3.7-flash-tiered"
