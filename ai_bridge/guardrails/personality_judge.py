"""Personality Fidelity Judge for PhoneAgent.

Asynchronously grades each conversation turn against the Persona Constitution
and task contract, computing an overall fidelity score without impacting live latency.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .permission_gate import PermissionGate

logger = logging.getLogger(__name__)

_EMPTY_ACK = re.compile(
    r"^(?:i hear you|i understand|i see|got it|that makes sense)\b",
    re.IGNORECASE,
)
_PRODUCT_QUESTION = re.compile(
    r"\b(?:how much|price|cost|what(?:'s| is) (?:in|included)|"
    r"what does .{0,30}(?:plan|package|subscription) include|"
    r"(?:starter|basic|essential|family|premium) (?:plan|package)|"
    r"trial|month[- ]to[- ]month|contract)\b",
    re.IGNORECASE,
)
_EVASIVE_FUTURE_ACTION = re.compile(
    r"\b(?:let me (?:pull up|look up|check|find)|i(?:'ll| will) (?:check|find)|"
    r"i can (?:certainly )?help you with that)\b",
    re.IGNORECASE,
)


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
        recent_ai_responses: tuple[str, ...] = (),
        call_direction: str = "",
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
        normalized_response = self._normalize(ai_response)
        repeated_turn = any(
            normalized_response and normalized_response == self._normalize(previous)
            for previous in recent_ai_responses
        )
        if repeated_turn:
            style_score = max(0.0, style_score - 15.0)
            feedback.append("Repeated an earlier AI turn verbatim")
        mirror_only = bool(
            _EMPTY_ACK.search(ai_response.strip())
            and "?" not in ai_response
            and not re.search(
                r"\b(?:essential|family|premium|trial|screens?|euros?|dollars?|"
                r"works on|includes?|recommend|option|i(?:'ll| will| can)|we (?:can|will)|"
                r"let's|next step|call back|send|schedule|confirm|means|you can)\b|\d",
                ai_response,
                re.IGNORECASE,
            )
        )
        if mirror_only:
            style_score = max(0.0, style_score - 15.0)
            feedback.append("Mirrored the caller without adding an answer or useful value")
        outbound_role_reset = bool(
            str(call_direction).lower() == "outbound"
            and re.search(r"\bhow can i help you today\b", ai_response, re.IGNORECASE)
        )
        if outbound_role_reset:
            style_score = max(0.0, style_score - 10.0)
            feedback.append("Used an inbound support greeting during an outbound call")

        # 2. Deterministic boundary compliance (35 points)
        compliant, violations = PermissionGate.check_compliance(ai_response)
        violations.extend(policy_violations or [])
        decision_score = max(0.0, 35.0 - (20.0 * len(violations)))
        feedback.extend(violations)

        # 3. Value alignment (20 points)
        value_score = 20.0
        normalized_response = ai_response.lower()
        repeated_apology = (
            "sorry" in normalized_response and "apologize" in normalized_response
        ) or ("désolé" in normalized_response and "excuse" in normalized_response)
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
        if repeated_turn:
            task_score = max(0.0, task_score - 15.0)
        if self._caller_requests_clarification(caller_input) and repeated_turn:
            task_score = 0.0
            feedback.append("Repeated instead of answering the caller's clarification")
        direct_product_question = bool(_PRODUCT_QUESTION.search(caller_input))
        evaded_product_question = bool(
            direct_product_question and _EVASIVE_FUTURE_ACTION.search(ai_response)
        )
        if evaded_product_question:
            task_score = 0.0
            feedback.append("Promised to look up an available product fact instead of answering")
        if mirror_only:
            task_score = min(task_score, 5.0)
        if outbound_role_reset:
            task_score = min(task_score, 5.0)

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

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(re.sub(r"[^\wÀ-ÿ\s]", " ", text.casefold()).split())

    @classmethod
    def _caller_requests_clarification(cls, text: str) -> bool:
        normalized = cls._normalize(text)
        return bool(
            re.search(
                r"\b(?:what do you mean|what does that mean|what improvements?|"
                r"can you explain|could you explain|say that again|repeat|clarify|"
                r"qu est ce que vous voulez dire|que voulez vous dire|expliquez|répétez)\b",
                normalized,
            )
        )
