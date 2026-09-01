"""Tests for delta-aware task state, stage progression and disposition.

These assert the behaviour that turns a listed objective into an executed one:
the agent knows what it already learned, asks for exactly one missing thing,
and ends with a recorded outcome instead of nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from phone_agent_gateway.ai_bridge.tasks.call_state import (
    CallOutcome,
    SlotSpec,
    StageSpec,
    TaskRuntime,
)
from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

CONTRACT: dict[str, Any] = {
    "id": "demo_task",
    "title": "Demo",
    "objective": "Demonstrate slots.",
    "inputs_required": [
        {
            "id": "permission",
            "question": "whether now is a good time",
            "detect": [r"\b(yes|oui|sure|go ahead)\b"],
        },
        {
            "id": "content",
            "question": "what they watch most",
            "detect": [r"\b(sport|football|films?|movies?)\b"],
        },
        {
            "id": "budget",
            "question": "what budget they have",
            "detect": [r"\b(budget|\d+\s*euros?)\b"],
        },
    ],
    "conversation_strategy": [
        "OPEN: Introduce and ask permission.",
        "DISCOVER: Ask one question at a time.",
        "CLOSE: Ask for a next step.",
    ],
    "knowledge": {"price": "Family is 15 euros a month."},
}


def test_slots_fill_from_the_callers_own_words() -> None:
    runtime = TaskRuntime(CONTRACT)
    actions = runtime.observe_caller_turn("Yes, go ahead.")

    assert actions.state_delta == {"permission": "Yes"}
    assert runtime.state["permission"] == "Yes"
    assert actions.changed is True


def test_an_answered_slot_is_never_asked_again() -> None:
    """Re-asking an answered question is what proves nobody was listening."""

    runtime = TaskRuntime(CONTRACT)
    runtime.observe_caller_turn("I mostly watch sport.")
    assert "content" in runtime.state
    assert "content" not in [slot.id for slot in runtime.missing_slots()]
    assert "content" not in runtime.brief().split("still_needed:")[1]


def test_the_brief_names_exactly_one_next_question() -> None:
    runtime = TaskRuntime(CONTRACT)
    runtime.observe_caller_turn("Yes, sure.")
    brief = runtime.brief()

    assert "already_answered" in brief
    assert "ask_next: what they watch most" in brief
    # Only the first missing slot is proposed, so it asks one thing at a time.
    assert brief.count("ask_next:") == 1


def test_the_brief_carries_facts_the_agent_may_state() -> None:
    """Without these the agent structurally cannot answer "how much?"."""

    runtime = TaskRuntime(CONTRACT)
    assert "Family is 15 euros a month." in runtime.brief()


def test_a_second_observation_does_not_refill_a_slot() -> None:
    runtime = TaskRuntime(CONTRACT)
    first = runtime.observe_caller_turn("Yes.")
    second = runtime.observe_caller_turn("Yes, definitely.")

    assert first.state_delta == {"permission": "Yes"}
    assert second.state_delta == {}, "an already-filled slot must not churn"


def test_empty_and_unmatched_turns_change_nothing() -> None:
    runtime = TaskRuntime(CONTRACT)
    assert runtime.observe_caller_turn("").changed is False
    assert runtime.observe_caller_turn("mm-hmm").state_delta == {}


def test_stage_advances_only_when_its_requirements_are_met() -> None:
    contract = dict(CONTRACT)
    contract["conversation_strategy"] = [
        {"name": "OPEN", "instruction": "Ask permission.", "requires": ["permission"]},
        {"name": "DISCOVER", "instruction": "Ask about content.", "requires": ["content"]},
        {"name": "CLOSE", "instruction": "Ask for a next step."},
    ]
    runtime = TaskRuntime(contract)
    assert runtime.stage == "OPEN"

    unrelated = runtime.observe_caller_turn("I watch football.")
    assert runtime.stage == "OPEN", "content does not satisfy OPEN"
    assert unrelated.stage_to == ""

    advanced = runtime.observe_caller_turn("Yes, go ahead.")
    assert advanced.stage_from == "OPEN"
    assert advanced.stage_to == "DISCOVER"
    assert runtime.stage == "DISCOVER"


def test_structured_stages_exclude_labelled_policy_rules() -> None:
    contract = dict(CONTRACT)
    contract["conversation_strategy"] = [
        {"name": "OPEN", "instruction": "Ask permission.", "requires": ["permission"]},
        "CALLER PRIORITY: Answer the caller first.",
        {"name": "DISCOVER", "instruction": "Learn content.", "requires": ["content"]},
        "OBJECTIONS: Answer the actual concern.",
        {"name": "RECOMMEND", "instruction": "Recommend a fit."},
    ]

    runtime = TaskRuntime(contract)

    assert [stage.name for stage in runtime.stages] == ["OPEN", "DISCOVER", "RECOMMEND"]
    runtime.observe_caller_turn("Yes, go ahead.")
    assert runtime.stage == "DISCOVER"
    runtime.observe_caller_turn("I watch football.")
    assert runtime.stage == "RECOMMEND"


def test_structured_stages_survive_studio_validation() -> None:
    contract = dict(CONTRACT)
    contract["conversation_strategy"] = [
        {"name": "OPEN", "instruction": "Ask permission.", "requires": ["permission"]},
        "CALLER PRIORITY: Answer the caller first.",
        {"name": "DISCOVER", "instruction": "Learn content.", "requires": ["content"]},
    ]

    validated = TaskEngine.validate_contract(contract)

    assert validated["conversation_strategy"][0] == {
        "name": "OPEN",
        "instruction": "Ask permission.",
        "requires": ["permission"],
    }
    assert validated["conversation_strategy"][1].startswith("CALLER PRIORITY")


def test_a_tool_can_write_state_directly() -> None:
    """The ADK pattern: a tool records what it learned, as a returned delta."""

    runtime = TaskRuntime(CONTRACT)
    actions = runtime.record("budget", "20 euros")

    assert actions.state_delta == {"budget": "20 euros"}
    assert runtime.state["budget"] == "20 euros"
    assert runtime.record("budget", "20 euros").changed is False


def test_outcome_is_recorded_rather_than_guessed() -> None:
    runtime = TaskRuntime(CONTRACT)
    assert runtime.outcome is CallOutcome.IN_PROGRESS

    runtime.set_outcome(CallOutcome.CALLBACK_REQUESTED)
    summary = runtime.summary()
    assert summary["outcome"] == "callback_requested"
    assert summary["task_id"] == "demo_task"
    assert set(summary["missing"]) == {"permission", "content", "budget"}


def test_summary_reports_what_was_collected() -> None:
    runtime = TaskRuntime(CONTRACT)
    runtime.observe_caller_turn("Yes, I watch sport, my budget is 20 euros.")
    summary = runtime.summary()

    assert summary["turns"] == 1
    assert set(summary["collected"]) == {"permission", "content", "budget"}
    assert summary["missing"] == []


def test_entering_an_unknown_stage_is_refused() -> None:
    runtime = TaskRuntime(CONTRACT)
    with pytest.raises(ValueError, match="unknown stage"):
        runtime.enter_stage("NOWHERE")


def test_plain_string_inputs_still_work() -> None:
    """Older contracts listed bare names; they must keep loading."""

    runtime = TaskRuntime({"id": "old", "inputs_required": ["budget", "devices"]})
    assert [slot.id for slot in runtime.slots] == ["budget", "devices"]
    assert runtime.observe_caller_turn("anything").state_delta == {}
    assert "still_needed: budget, devices" in runtime.brief()


def test_stage_parsing_ignores_prose_without_a_name() -> None:
    assert StageSpec.parse("OPEN: do the thing").name == "OPEN"
    assert StageSpec.parse("just some prose") is None


def test_a_slot_needs_an_id() -> None:
    with pytest.raises(ValueError, match="needs an id"):
        SlotSpec.parse({"question": "no id here"})


def test_an_invalid_detect_pattern_is_refused_at_save_time() -> None:
    """A broken pattern must fail when authored, not mid-call."""

    with pytest.raises(ValueError, match="not valid"):
        TaskEngine.validate_contract(
            {
                "id": "bad_task",
                "title": "t",
                "objective": "o",
                "inputs_required": [{"id": "x", "detect": ["\\b(unclosed"]}],
            }
        )


def test_duplicate_slots_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate required input"):
        TaskEngine.validate_contract(
            {
                "id": "dupe_task",
                "title": "t",
                "objective": "o",
                "inputs_required": [{"id": "budget"}, {"id": "budget"}],
            }
        )


def test_shipped_iptv_contract_declares_slots_and_facts() -> None:
    contract = TaskEngine().require_contract("iptv_subscription_sales")
    runtime = TaskRuntime(contract)

    assert len(runtime.slots) >= 5
    assert runtime.knowledge, "the agent must be able to answer 'how much?'"
    # A real caller sentence fills a real slot.
    assert runtime.observe_caller_turn("Je regarde surtout le sport.").state_delta


def test_shipped_iptv_contract_recognizes_real_call_answers() -> None:
    runtime = TaskRuntime(TaskEngine().require_contract("iptv_subscription_sales"))

    runtime.record("permission_to_continue", "granted")
    viewing = runtime.observe_caller_turn("I mostly use Netflix and YouTube.")
    budget = runtime.observe_caller_turn("I want to stay under 20.")

    assert viewing.state_delta["viewing_preferences"] == "Netflix"
    assert budget.state_delta["budget_or_purchase_priority"] == "under 20"
    assert runtime.stage == "RECOMMEND"
