"""Evaluation taxonomy, automated testing, and release gates.

Governed by Milestone 14 (M14-01 through M14-12):
Defines deterministic evaluators, release thresholds, and regression gates.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvaluationScenario(StrictModel):
    scenario_id: str
    expected_substrings: list[str] = Field(default_factory=list)
    forbidden_substrings: list[str] = Field(default_factory=list)
    max_latency_ms: float = 1200.0


class EvaluationResult(StrictModel):
    scenario_id: str
    verdict: Literal["pass", "fail"]
    failures: list[str] = Field(default_factory=list)


def evaluate_agent_turn(
    scenario: EvaluationScenario,
    spoken_response: str,
    latency_ms: float,
) -> EvaluationResult:
    """Deterministic evaluation of turn quality and release thresholds (M14-02, M14-12)."""
    failures = []
    for exp in scenario.expected_substrings:
        if exp.lower() not in spoken_response.lower():
            failures.append(f"Missing expected fact: '{exp}'")
    for forb in scenario.forbidden_substrings:
        if forb.lower() in spoken_response.lower():
            failures.append(f"Contains forbidden content: '{forb}'")
    if latency_ms > scenario.max_latency_ms:
        failures.append(f"Latency {latency_ms}ms exceeded budget of {scenario.max_latency_ms}ms")

    return EvaluationResult(
        scenario_id=scenario.scenario_id,
        verdict="pass" if not failures else "fail",
        failures=failures,
    )
