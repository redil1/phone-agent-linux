"""Direction-aware cold-prospecting and inbound-intent policy tests."""

from __future__ import annotations

import pytest

from phone_agent_gateway.ai_bridge.call_context import (
    CallContextPolicy,
    InterestState,
    ProspectingPhase,
)


def test_outbound_permission_is_not_product_interest() -> None:
    context = CallContextPolicy("outbound")

    assert context.observe_caller_turn("Yes.", permission_state="granted") is True
    assert context.phase is ProspectingPhase.RELEVANCE_DISCOVERY
    assert context.interest is InterestState.UNKNOWN
    assert context.product_qualification_unlocked is False
    state = context.state_block("Which device do you use?")
    assert "product_qualification_unlocked: no" in state
    assert "locked_until_explicit_interest" in state
    assert "current" in state


def test_outbound_develops_need_then_checks_interest_before_qualification() -> None:
    context = CallContextPolicy("outbound")
    context.observe_caller_turn("Yes", permission_state="granted")

    context.observe_caller_turn(
        "I currently use normal cable television.", permission_state="granted"
    )
    assert context.phase is ProspectingPhase.NEED_DEVELOPMENT
    assert context.interest is InterestState.NEED_SIGNAL
    assert context.product_qualification_unlocked is False

    context.observe_caller_turn(
        "It is expensive and I miss football matches.", permission_state="granted"
    )
    assert context.phase is ProspectingPhase.INTEREST_CHECK
    move, question = context.steering("Which device do you use?")
    assert "verified outcome" in move
    assert question == "locked_until_explicit_interest"

    context.observe_caller_turn("Yes, that sounds useful.", permission_state="granted")
    assert context.phase is ProspectingPhase.PRODUCT_QUALIFICATION
    assert context.interest is InterestState.INTERESTED
    assert context.product_qualification_unlocked is True
    _, question = context.steering("Which device do you use?")
    assert question == "Which device do you use?"


def test_explicit_interest_can_unlock_qualification_immediately() -> None:
    context = CallContextPolicy("outbound")
    context.observe_caller_turn("Tell me more, what do you offer?", permission_state="unknown")

    assert context.interest is InterestState.INTERESTED
    assert context.product_qualification_unlocked is True

    second = CallContextPolicy("outbound")
    second.observe_caller_turn(
        "Yes, a more reliable single option would be interesting.",
        permission_state="granted",
    )
    assert second.interest is InterestState.INTERESTED
    assert second.product_qualification_unlocked is True


def test_outbound_refusal_closes_without_another_sales_question() -> None:
    context = CallContextPolicy("outbound")
    context.observe_caller_turn("No, not interested.", permission_state="refused")

    assert context.phase is ProspectingPhase.CLOSE
    assert context.interest is InterestState.NOT_INTERESTED
    move, question = context.steering("Which package do you want?")
    assert "Close politely" in move
    assert question == "locked"


def test_inbound_call_starts_with_caller_intent_and_direct_discovery() -> None:
    context = CallContextPolicy("inbound")

    assert context.phase is ProspectingPhase.INTENT_DISCOVERY
    assert context.interest is InterestState.CALLER_INITIATED
    assert context.product_qualification_unlocked is True
    assert "caller initiated" in context.base_instructions().lower()
    _, question = context.steering("How can I help?")
    assert question == "How can I help?"


def test_inbound_greeting_ignores_outbound_sales_opening() -> None:
    inbound = CallContextPolicy("inbound")
    outbound = CallContextPolicy("outbound")
    configured = "Hello, this is {name}. I'm calling about a product. Is now a good time?"

    inbound_greeting = inbound.opening_greeting(
        name="Adam",
        role="Senior Sales Consultant at IPTV Shopping",
        language="en-US",
        configured_outbound=configured,
    )
    outbound_greeting = outbound.opening_greeting(
        name="Adam",
        role="Senior Sales Consultant at IPTV Shopping",
        language="en-US",
        configured_outbound=configured,
    )

    assert inbound_greeting == (
        "Hello, this is Adam from IPTV Shopping. Thanks for calling. How can I help?"
    )
    assert "I'm calling about a product" not in inbound_greeting
    assert outbound_greeting == configured.replace("{name}", "Adam")


def test_unknown_call_direction_is_rejected() -> None:
    with pytest.raises(ValueError, match="inbound or outbound"):
        CallContextPolicy("sideways")
