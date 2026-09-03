"""Turn intelligence, duplex audio, and STT controller.

Governed by Milestone 6 (M6-01 through M6-14):
Defines unified turn controller, acoustic epoch tracking, fragment elimination,
echo rejection, and standardized STT adapter interfaces.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TurnHypothesis(StrictModel):
    transcript: str
    is_final: bool
    confidence: float = Field(ge=0.0, le=1.0)
    epoch: int = Field(ge=0)


class UnifiedTurnController:
    """Unified turn controller (M6-01).

    Owns speech epochs, provisional hypotheses, end-of-turn, echo rejection,
    and single authoritative turn publication.
    """

    def __init__(self, echo_threshold_similarity: float = 0.85) -> None:
        self.current_epoch: int = 0
        self.recent_agent_utterances: list[str] = []
        self.pending_fragments: list[str] = []
        self.echo_threshold = echo_threshold_similarity

    def register_agent_speech(self, text: str) -> None:
        """Register downlink agent speech for echo rejection (M6-09)."""
        normalized = " ".join(text.lower().strip().split())
        if normalized:
            self.recent_agent_utterances.append(normalized)
            if len(self.recent_agent_utterances) > 10:
                self.recent_agent_utterances.pop(0)

    def is_echo(self, hypothesis: str) -> bool:
        """Reject acoustic loopback/echo of agent downlink (M6-09)."""
        normalized = " ".join(hypothesis.lower().strip().split())
        for utterance in self.recent_agent_utterances:
            if normalized in utterance or utterance in normalized:
                return True
        return False

    def process_fragment(self, fragment: str, is_final: bool, confidence: float) -> TurnHypothesis | None:
        """Eliminate fragmentation and publish single turn (M6-05, M6-06)."""
        clean = fragment.strip()
        if not clean:
            return None

        if self.is_echo(clean):
            return None  # Suppress echo

        self.pending_fragments.append(clean)
        if is_final:
            full_turn = " ".join(self.pending_fragments)
            self.pending_fragments = []
            turn = TurnHypothesis(
                transcript=full_turn,
                is_final=True,
                confidence=confidence,
                epoch=self.current_epoch,
            )
            self.current_epoch += 1
            return turn
        return None
