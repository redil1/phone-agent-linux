"""Small semantic turn guard for fragments that must not trigger an AI reply."""

from __future__ import annotations

import logging
import re

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("PhoneAgentTurnContinuity")

# These are grammatical fragments, not useful standalone caller turns. Keep the
# list intentionally small: one-word answers such as yes/no/sport must continue
# to reach the model immediately.
_STANDALONE_CONNECTORS = frozenset(
    {
        "and",
        "because",
        "but",
        "or",
        "the",
        "to",
        "avec",
        "car",
        "de",
        "des",
        "donc",
        "du",
        "et",
        "mais",
        "parce",
        "pour",
        "que",
        "ديال",
        "ديالي",
        "ديالو",
        "ديالها",
    }
)


def _normalized_tokens(text: str) -> list[str]:
    return re.findall(r"[\wÀ-ÿ\u0600-\u06ff']+", text.casefold(), flags=re.UNICODE)


def is_semantically_incomplete_fragment(text: str) -> bool:
    """Return true only for a high-confidence, non-actionable speech fragment."""

    tokens = _normalized_tokens(text)
    if not tokens:
        return True
    if len(tokens) == 1:
        return tokens[0] in _STANDALONE_CONNECTORS
    return False


def looks_semantically_incomplete(text: str) -> bool:
    """Endpointing hint for fragments or sentences that clearly trail off."""

    normalized = " ".join(text.strip().casefold().split())
    if not normalized or normalized.endswith((".", "!", "?")):
        return False
    # A clause left hanging on continuation punctuation is mid-sentence whatever
    # word precedes it. "Hello," is the caller drawing breath, not a turn, and
    # committing it made the agent answer a greeting fragment.
    if normalized.endswith((",", ";", ":", "-", "—")):
        return True
    if is_semantically_incomplete_fragment(normalized):
        return True
    return bool(
        re.search(
            r"(?:because|why|if|but|so|when|where|how|parce que|pourquoi|si|mais|"
            r"donc|que|quand|où|comment|and|or|to|for|like|such as|go|aller|"
            r"comme)\s*[,;:]*$",
            normalized,
        )
    )


class SemanticTurnGuardProcessor(FrameProcessor):
    """Prevent incomplete final transcripts from becoming artificial LLM turns."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            direction is FrameDirection.DOWNSTREAM
            and isinstance(frame, TranscriptionFrame)
            and is_semantically_incomplete_fragment(frame.text)
        ):
            logger.warning(
                "Suppressed semantically incomplete caller fragment chars=%d",
                len(frame.text.strip()),
            )
            return
        await self.push_frame(frame, direction)
