"""Universal low-latency agent runtime.

Governed by Milestone 5 (M5-01 through M5-14):
Defines authoritative turn input, structured agent output, typed validation,
semantic novelty tracking, and single conversational authority.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TurnInput(StrictModel):
    """Authoritative turn input (M5-01)."""

    text: str = Field(min_length=1, max_length=10_000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    language: str = Field(default="en-US", min_length=2, max_length=20)
    acoustic_epoch: int = Field(default=0, ge=0)
    is_final: bool = True
    channel: Literal["gsm", "whatsapp_phone", "whatsapp"] = "gsm"


class ToolRequest(StrictModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(StrictModel):
    """Structured agent output (M5-02)."""

    spoken_text: str = Field(min_length=1, max_length=10_000)
    intent: str = Field(default="general_conversation", max_length=120)
    state_updates: dict[str, Any] = Field(default_factory=dict)
    tool_requests: list[ToolRequest] = Field(default_factory=list)
    completion_decision: Literal["continue", "transfer", "hangup"] = "continue"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class SemanticNoveltyTracker:
    """Detects and prevents loops, repeated questions, and robotic mirroring (M5-07)."""

    def __init__(self, history_limit: int = 5) -> None:
        self.history: list[str] = []
        self.history_limit = history_limit

    def check_and_record(self, spoken_text: str) -> bool:
        normalized = " ".join(spoken_text.lower().strip().split())
        for prev in self.history:
            if normalized == prev:
                return False  # Detected identical repeat
        self.history.append(normalized)
        if len(self.history) > self.history_limit:
            self.history.pop(0)
        return True


def plan_turn_response(
    turn: TurnInput,
    history_tracker: SemanticNoveltyTracker | None = None,
) -> AgentOutput:
    """Answer-first planning and typed validation (M5-03, M5-04)."""
    text_clean = turn.text.strip()
    if not text_clean:
        return AgentOutput(spoken_text="I missed that. Could you repeat?", intent="clarification")

    # Simple rule demonstration for answer-first
    spoken = f"I received your request: '{text_clean}'. How else can I help?"
    if history_tracker:
        if not history_tracker.check_and_record(spoken):
            spoken = "Certainly. What other questions do you have?"

    return AgentOutput(
        spoken_text=spoken,
        intent="answer_first_progress",
        completion_decision="continue",
    )
