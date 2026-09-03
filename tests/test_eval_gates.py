"""Tests for Evaluation and Release Gates (Milestone 14)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.eval_gates import (
    EvaluationScenario,
    evaluate_agent_turn,
)


def test_turn_evaluation_gate() -> None:
    scenario = EvaluationScenario(
        scenario_id="price_inquiry",
        expected_substrings=["$25"],
        forbidden_substrings=["password", "secret"],
        max_latency_ms=800.0,
    )

    # Passing turn
    res_pass = evaluate_agent_turn(
        scenario,
        spoken_response="The subscription is $25 per month.",
        latency_ms=450.0,
    )
    assert res_pass.verdict == "pass"
    assert len(res_pass.failures) == 0

    # Failing turn (forbidden word + latency violation)
    res_fail = evaluate_agent_turn(
        scenario,
        spoken_response="The password is secret and costs $25.",
        latency_ms=950.0,
    )
    assert res_fail.verdict == "fail"
    assert len(res_fail.failures) == 3
