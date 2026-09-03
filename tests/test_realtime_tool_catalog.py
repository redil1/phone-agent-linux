"""Spoken facts must come from a verified tool result, never from memory."""

from __future__ import annotations

import asyncio
import json

import pytest

from phone_agent_gateway.ai_bridge.agent_policy import AgentPolicyRuntime
from phone_agent_gateway.ai_bridge.tasks.tool_catalog import (
    INLINE_KNOWLEDGE_BUDGET_CHARS,
    build_end_call_tool,
    build_tool_catalog,
    execute_tool,
    tool_definitions,
)


def policy() -> AgentPolicyRuntime:
    return AgentPolicyRuntime(
        caller_id="+33123456789",
        task_id="iptv_subscription_sales",
        language="en-US",
        additional_instructions="",
        memory_enabled=False,
    )


def catalog(runtime: AgentPolicyRuntime):
    return build_tool_catalog(runtime.task_contract, runtime.task)


def retrieval_catalog(runtime: AgentPolicyRuntime):
    """A knowledge base too large to inline, where lookups earn their round trip."""

    knowledge = dict(runtime.task_contract["knowledge"])
    for index in range(120):
        knowledge[f"channel_pack_{index}"] = (
            "A verified description of one more channel package offering."
        )
    contract = {**runtime.task_contract, "knowledge": knowledge}
    assert (
        sum(len(key) + len(value) for key, value in knowledge.items())
        > INLINE_KNOWLEDGE_BUDGET_CHARS
    ), "fixture must outgrow the inline budget for retrieval to switch on"
    return build_tool_catalog(contract, runtime.task)


def call(tools, name: str, **arguments) -> dict:
    return json.loads(asyncio.run(execute_tool(tools, name, json.dumps(arguments))))


def test_only_contract_allowed_tools_are_offered() -> None:
    runtime = policy()
    tools = catalog(runtime)
    allowed = set(runtime.task_contract["allowed_tools"])
    assert set(tools) <= allowed
    # Advertising a tool that is not registered makes the model invent calls.
    assert set(tools) == runtime.available_tools or set(tools) <= allowed


def test_every_definition_is_a_valid_realtime_function() -> None:
    definitions = tool_definitions(catalog(policy()))
    assert definitions
    for definition in definitions:
        assert definition["type"] == "function"
        assert definition["name"]
        assert definition["description"]
        parameters = definition["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        for name in parameters["required"]:
            assert name in parameters["properties"]


def test_end_call_is_a_bounded_model_decision_not_a_phrase_matcher() -> None:
    tool = build_end_call_tool()
    definition = tool.definition
    assert definition["name"] == "end_call"
    assert set(definition["parameters"]["required"]) == {"reason", "closing_message"}
    result = json.loads(
        asyncio.run(
            execute_tool(
                {tool.name: tool},
                tool.name,
                json.dumps(
                    {
                        "reason": "The conversation is naturally complete.",
                        "closing_message": "Thank you for calling. Goodbye.",
                    }
                ),
            )
        )
    )
    assert result == {
        "accepted": True,
        "reason": "The conversation is naturally complete.",
        "closing_message": "Thank you for calling. Goodbye.",
        "status": "closing_message_will_be_spoken_before_hangup",
    }


def test_end_call_rejects_missing_control_data_without_hanging_up() -> None:
    tool = build_end_call_tool()
    result = json.loads(asyncio.run(execute_tool({tool.name: tool}, tool.name, "{}")))
    assert result["accepted"] is False
    assert result["error"] == "reason is required"


def test_lookups_are_not_offered_when_the_facts_are_already_in_the_prompt() -> None:
    """compile_realtime() inlines the knowledge block, so a lookup returns what
    the model already holds while costing a second inference: on the call that
    was heard as a spoken preamble followed by seconds of dead air."""

    tools = catalog(policy())
    assert "subscription_plan_lookup" not in tools
    assert "knowledge_base_search" not in tools
    # Actions still need tools: they do something the prompt cannot.
    assert "callback_schedule" in tools


def test_lookups_return_once_the_knowledge_outgrows_the_prompt() -> None:
    tools = retrieval_catalog(policy())
    assert "subscription_plan_lookup" in tools
    assert "knowledge_base_search" in tools


def test_plan_lookup_returns_only_verified_contract_prices() -> None:
    runtime = policy()
    result = call(retrieval_catalog(runtime), "subscription_plan_lookup", plan="family")
    assert result["found"] is True
    assert result["plans"]["family"] == runtime.task_contract["knowledge"]["family_price"]


def test_knowledge_search_matches_whole_words_not_substrings() -> None:
    """"Android" once matched the term "and" and buried the real answer."""

    result = call(
        retrieval_catalog(policy()), "knowledge_base_search", query="trial length and availability"
    )
    assert result["found"] is True
    assert result["facts"][0]["topic"] == "trial"


def test_unknown_facts_are_reported_rather_than_improvised() -> None:
    result = call(
        retrieval_catalog(policy()), "knowledge_base_search", query="8K holographic broadcasts"
    )
    assert result["found"] is False
    assert "improvise" in result["guidance"]


def test_writes_are_not_offered_as_tools() -> None:
    """A tool the model gains nothing from costs a whole extra inference.

    lead_capture fired on every discovery answer, adding a spoken preamble and
    seconds of dead air before the real reply. Slots fill from the transcript.
    """

    runtime = policy()
    tools = catalog(runtime)
    assert "lead_capture" not in tools

    runtime.task.observe_caller_turn("I mostly watch live football matches")
    assert runtime.task.state.get("viewing_preferences")


def test_approval_gated_actions_never_report_success() -> None:
    runtime = policy()
    tools = catalog(runtime)
    for action in runtime.task_contract["approval_required"]:
        if action not in tools:
            continue
        result = call(tools, action, summary="Family plan agreed")
        assert result["completed"] is False
        assert result["reason"] == "requires_authorized_operator"


def test_callback_is_recorded_without_claiming_a_confirmed_appointment() -> None:
    result = call(catalog(policy()), "callback_schedule", when="tomorrow morning")
    assert result["recorded"] is True
    assert result["status"] == "noted_for_operator_confirmation"


@pytest.mark.parametrize(
    "raw",
    ["not json at all", "[1, 2, 3]", ""],
)
def test_malformed_arguments_never_raise_into_the_call(raw: str) -> None:
    result = json.loads(
        asyncio.run(execute_tool(retrieval_catalog(policy()), "knowledge_base_search", raw))
    )
    assert "error" in result or result.get("found") is False


def test_unknown_tool_name_is_reported_not_raised() -> None:
    result = json.loads(asyncio.run(execute_tool(catalog(policy()), "wire_transfer", "{}")))
    assert "unknown tool" in result["error"]


def test_tool_guidance_matches_the_tools_that_are_actually_registered() -> None:
    """Telling the model to quote only from a lookup that does not exist made it
    refuse a price it was holding, then quote one on the next question."""

    runtime = policy()
    inline = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract,
        language="en-US",
        additional_instructions="",
        available_tools=set(catalog(runtime)),
    )
    assert "there is no lookup to wait for" in inline
    assert "Look up a fact before quoting it" not in inline

    with_retrieval = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract,
        language="en-US",
        additional_instructions="",
        available_tools=set(retrieval_catalog(runtime)),
    )
    assert "Look up a fact before quoting it" in with_retrieval
    assert "there is no lookup to wait for" not in with_retrieval


def test_tool_guidance_bans_the_preamble_fillers_the_model_invented() -> None:
    runtime = policy()
    instructions = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract,
        language="en-US",
        additional_instructions="",
        available_tools=set(catalog(runtime)),
    )
    assert "let me think" in instructions
    assert "NEVER announce or narrate a tool" in instructions


def test_unclear_audio_is_separated_from_ambiguous_meaning() -> None:
    """"Sorry, I didn't catch that" fired on speech that was heard perfectly."""

    runtime = policy()
    instructions = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract,
        language="en-US",
        additional_instructions="",
        available_tools=set(),
    )
    assert "# UNCLEAR AUDIO — only when you could not HEAR the words" in instructions
    assert "# AMBIGUOUS MEANING" in instructions
    ambiguous = instructions[instructions.index("# AMBIGUOUS MEANING") :]
    assert "do not suggest the line or their speech was the problem" in ambiguous


def test_the_prompt_directs_pace_and_authority_not_just_warmth() -> None:
    """speed only changes playback rate, so real pace has to be instructed."""

    runtime = policy()
    instructions = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract,
        language="en-US",
        additional_instructions="",
        available_tools=set(),
    )
    assert "# PACING" in instructions
    assert "do not sound rushed" in instructions
    assert "# AUTHORITY" in instructions
    # The call where a trust objection was answered with a cheaper plan, twice.
    assert "Answer the objection the caller actually raised" in instructions
    assert "No hedging" in instructions


def test_sample_phrases_model_expert_delivery_and_ask_for_variety() -> None:
    """The model imitates these closely, so they must show authority, not only mechanics."""

    runtime = policy()
    for language, price_cue in (("en-US", "fifty-nine dollars"), ("fr-FR", "cinquante-neuf")):
        instructions = runtime.persona_compiler.compile_realtime(
            task_contract=runtime.task_contract,
            language=language,
            additional_instructions="",
            available_tools=set(),
        )
        assert "VARY YOUR RESPONSES" in instructions
        assert price_cue in instructions
        assert "Meeting doubt about you, not about price" in instructions


def test_question_variety_never_blocks_needed_clarification() -> None:
    """Four turns, four questions, is an interrogation rather than a conversation.

    The rule has to outrank the several sections that each tell the agent to ask
    something, or it competes with them and loses.
    """

    runtime = policy()
    instructions = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract,
        language="en-US",
        additional_instructions="",
        available_tools=set(),
    )
    priority = instructions[instructions.index("# NON-NEGOTIABLE TURN PRIORITY") :]
    budget = priority[: priority.index("\n# ")]
    assert "Avoid interrogating" in budget
    assert "clarification is genuinely needed" in budget
    assert "MUST NOT END IN ONE" not in instructions
    # Nothing below may still be issuing a bare order to ask.
    assert "move directly to one useful discovery question" not in instructions


def test_turn_shape_teaches_reacting_and_stopping() -> None:
    runtime = policy()
    instructions = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract,
        language="en-US",
        additional_instructions="",
        available_tools=set(),
    )
    assert "# TURN SHAPE" in instructions
    assert "A turn that ends in a statement is a good turn" in instructions
    assert "Reuse their terminology" in instructions
    assert "never echo their whole" in instructions
    # Examples of the shape, not only of the wording.
    assert "Reacting, with no question at all" in instructions
    assert "Ending a turn on a statement" in instructions


def test_turn_shape_examples_exist_in_both_languages() -> None:
    runtime = policy()
    for language, cue in (("en-US", "that's the easy one"), ("fr-FR", "le plus simple")):
        instructions = runtime.persona_compiler.compile_realtime(
            task_contract=runtime.task_contract,
            language=language,
            additional_instructions="",
            available_tools=set(),
        )
        assert cue in instructions


def test_a_commitment_needs_an_intelligible_yes() -> None:
    """"Ja, ja noch" was read as agreement and the agent offered checkout."""

    runtime = policy()
    instructions = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract, language="en-US",
        additional_instructions="", available_tools=set(),
    )
    assert "# COMMITMENT REQUIRES AN INTELLIGIBLE YES" in instructions
    assert "is not a yes" in instructions
    assert "Never advance" in instructions
    # It must outrank the closing stage, not sit below it as a suggestion.
    assert instructions.index("COMMITMENT REQUIRES") < instructions.index("# CONVERSATION FLOW")


def test_an_unsupported_language_is_answered_by_asking_not_guessing() -> None:
    """Darija in Arabic script was labelled French and answered in French."""

    runtime = policy()
    instructions = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract, language="en-US",
        additional_instructions="", available_tools=set(),
    )
    assert "# A LANGUAGE YOU DO NOT SPEAK" in instructions
    assert "do NOT answer in either as though you understood" in instructions
    assert "ask which they prefer" in instructions


def test_french_uses_formal_address() -> None:
    """"Tu préfères quelle durée ?" is informal for a cold business call."""

    runtime = policy()
    for language in ("en-US", "fr-FR"):
        instructions = runtime.persona_compiler.compile_realtime(
            task_contract=runtime.task_contract, language=language,
            additional_instructions="", available_tools=set(),
        )
        assert '"vous", never "tu"' in instructions
    french = runtime.persona_compiler.compile_realtime(
        task_contract=runtime.task_contract, language="fr-FR",
        additional_instructions="", available_tools=set(),
    )
    assert "Tu préfères" not in french
