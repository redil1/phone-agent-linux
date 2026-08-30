"""Personality Fidelity Judge for PhoneAgent.

Asynchronously grades each conversation turn against the Persona Constitution
and task contract, computing an overall fidelity score without impacting live latency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .permission_gate import PermissionGate

logger = logging.getLogger(__name__)


@dataclass
class TurnEvaluationResult:
    """Detailed scoring metrics for a single conversation turn."""

    overall_score: float
    decision_similarity_score: float
    value_alignment_score: float
    communication_style_score: float
    task_performance_score: float
    passed: bool
    feedback: list[str]


class PersonalityFidelityJudge:
    """Evaluates conversation turns against the Persona specification."""

    def __init__(self) -> None:
        self.min_pass_score: float = 85.0

    def evaluate_turn(
        self,
        *,
        caller_input: str,
        ai_response: str,
        persona_data: dict[str, Any] | None = None,
        task_contract: dict[str, Any] | None = None,
        policy_violations: list[str] | None = None,
    ) -> TurnEvaluationResult:
        """Score a speech turn across 4 key dimensions."""
        feedback: list[str] = []

        # 1. Communication style (20 points)
        style_score = 20.0
        word_count = len(ai_response.split())
        if word_count > 35:
            style_score -= 10.0
            feedback.append(f"Response too long for telephony ({word_count} words)")
        if any(c in ai_response for c in ["*", "#", "`", "•"]):
            style_score -= 10.0
            feedback.append("Contained markdown formatting characters")

        # 2. Deterministic boundary compliance (35 points)
        compliant, violations = PermissionGate.check_compliance(ai_response)
        violations.extend(policy_violations or [])
        decision_score = max(0.0, 35.0 - (20.0 * len(violations)))
        feedback.extend(violations)

        # 3. Value alignment (20 points)
        value_score = 20.0
        normalized_response = ai_response.lower()
        repeated_apology = (
            ("sorry" in normalized_response and "apologize" in normalized_response)
            or ("désolé" in normalized_response and "excuse" in normalized_response)
        )
        if repeated_apology:
            value_score -= 5.0
            feedback.append("Repeated unnecessary apologies")

        # 4. Task-contract adherence (25 points). Actual external task success is
        # only reported by tools, not guessed by this lightweight judge.
        task_score = 25.0 if task_contract else 15.0
        if not ai_response.strip():
            task_score = 0.0
            feedback.append("Empty response")
        if not caller_input.strip():
            task_score = max(0.0, task_score - 5.0)
            feedback.append("No finalized caller input was available for this response")

        total = decision_score + value_score + style_score + task_score
        passed = total >= self.min_pass_score and compliant and not violations

        return TurnEvaluationResult(
            overall_score=round(total, 1),
            decision_similarity_score=round(decision_score, 1),
            value_alignment_score=round(value_score, 1),
            communication_style_score=round(style_score, 1),
            task_performance_score=round(task_score, 1),
            passed=passed,
            feedback=feedback,
        )
