"""Tests for Streaming TTS and Verified Speech Delivery (Milestone 8)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.speech_delivery import (
    SpeechDeliverySupervisor,
    UniversalTextNormalizer,
)


def test_text_normalizer_expands_currency_and_acronyms() -> None:
    normalizer = UniversalTextNormalizer()
    text = "The IPTV plan costs $25 per month."
    normalized = normalizer.normalize(text)
    assert normalized == "The I P T V plan costs 25 dollars per month."


def test_speech_delivery_supervisor_lifecycle() -> None:
    sup = SpeechDeliverySupervisor()
    assert sup.state == "idle"

    sup.on_synthesis_generated()
    assert sup.state == "generated"

    sup.on_queued()
    assert sup.state == "queued"

    sup.on_sent()
    assert sup.state == "sent"

    sup.on_rendered(char_count=45)
    assert sup.state == "rendered"
    assert sup.delivered_characters == 45

    sup.on_interrupted()
    assert sup.state == "interrupted"
