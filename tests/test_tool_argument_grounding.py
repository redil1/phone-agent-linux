from __future__ import annotations

import json

from phone_agent_gateway.ai_bridge.tool_argument_grounding import ground_tool_arguments


def test_support_ticket_literals_are_copied_from_authoritative_transcript() -> None:
    result = ground_tool_arguments(
        "business_create_support_ticket",
        json.dumps(
            {
                "subject": "My complete test",
                "description": "Installation is working.",
                "priority": "Low",
                "category": "Installation",
            }
        ),
        "Create a support ticket named Mac complete test saying the installation is working.",
    )

    assert json.loads(result.raw_arguments) == {
        "subject": "Mac complete test",
        "description": "the installation is working.",
        "priority": "Low",
        "category": "Installation",
    }
    assert result.grounded_fields == ("subject", "description")
    assert result.blocked is False


def test_low_confidence_literal_mismatch_is_blocked_before_write() -> None:
    result = ground_tool_arguments(
        "business_create_support_ticket",
        '{"subject":"My complete test","description":"Installation is working."}',
        "Create a support ticket named Mac complete test saying the installation is working.",
        transcript_trusted=False,
    )

    blocked = json.loads(result.blocked_output())
    assert result.blocked is True
    assert blocked["executed"] is False
    assert blocked["error"] == "literal_confirmation_required"
    assert blocked["fields"] == ["subject", "description"]


def test_french_ticket_literals_are_preserved() -> None:
    result = ground_tool_arguments(
        "business_create_support_ticket",
        '{"subject":"Test de mon Mac","description":"Tout fonctionne"}',
        "Crée un ticket intitulé Test Mac disant que l'installation fonctionne.",
    )

    arguments = json.loads(result.raw_arguments)
    assert arguments["subject"] == "Test Mac"
    assert arguments["description"] == "que l'installation fonctionne."


def test_ticket_title_survives_a_split_multi_turn_request() -> None:
    result = ground_tool_arguments(
        "business_create_support_ticket",
        '{"subject":"my complaint test","description":"installation is working"}',
        "Say into installation is working.",
        caller_turns=(
            ("Create a support ticket named Mac complete test.", True),
            ("Say into installation is working.", True),
        ),
    )

    assert json.loads(result.raw_arguments) == {
        "subject": "Mac complete test",
        "description": "installation is working.",
    }
    assert result.grounded_fields == ("subject", "description")


def test_whatsapp_dictation_is_preserved_without_changing_tool_selection() -> None:
    result = ground_tool_arguments(
        "whatsapp_send_text_current_customer",
        '{"text":"PhoneAgent test was successful."}',
        "Send me a WhatsApp message saying phone agent complete is successful.",
    )

    assert json.loads(result.raw_arguments) == {
        "text": "phone agent complete is successful."
    }
    assert result.grounded_fields == ("text",)


def test_compound_whatsapp_request_is_not_replaced_with_command_text() -> None:
    raw = '{"text":"Phone agent complete test successful. Ticket number: 0005."}'
    result = ground_tool_arguments(
        "whatsapp_send_text_current_customer",
        raw,
        (
            "Send me a WhatsApp message saying phone agent complete test successful "
            "and send me via WhatsApp also the ticket number."
        ),
    )

    assert result.raw_arguments == raw
    assert result.grounded_fields == ()


def test_unmarked_or_unrelated_arguments_are_not_rewritten() -> None:
    raw = '{"subject":"Network issue","description":"The screen is blank."}'
    result = ground_tool_arguments(
        "business_create_support_ticket",
        raw,
        "Please help me because the screen is blank.",
    )
    assert result.raw_arguments == raw
    assert result.grounded_fields == ()

    unrelated = ground_tool_arguments(
        "business_create_opportunity",
        '{"title":"Mac complete test"}',
        "Create an opportunity named something else.",
    )
    assert unrelated.raw_arguments == '{"title":"Mac complete test"}'
