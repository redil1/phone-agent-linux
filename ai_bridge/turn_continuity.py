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


_CONCISE_TERMINAL = frozenset(
    {
        "yes",
        "no",
        "yeah",
        "yep",
        "nope",
        "okay",
        "ok",
        "sure",
        "fine",
        "right",
        "good",
        "perfect",
        "thanks",
        "oui",
        "non",
        "ouais",
        "d accord",
        "bien",
        "parfait",
        "merci",
        "exactement",
        "voila",
        "voilà",
        "allô",
        "allo",
        "hello",
        "hi",
    }
)


def is_concise_terminal_turn(text: str) -> bool:
    """Return true for brief, unambiguous confirmation/negation turns."""
    tokens = _normalized_tokens(text)
    if not tokens:
        return False
    if len(tokens) <= 2 and all(t in _CONCISE_TERMINAL for t in tokens):
        return True
    return False


def looks_semantically_incomplete(text: str) -> bool:
    """Endpointing hint for fragments or sentences that clearly trail off."""

    normalized = " ".join(text.strip().casefold().split())
    if not normalized or normalized.endswith((".", "!", "?")):
        return False
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


def dynamic_endpoint_delay_ms(partial_text: str, base_silence_ms: int = 700) -> int:
    """Calculate the optimal silence patience based on clause semantics."""
    if not partial_text:
        return base_silence_ms
    if is_concise_terminal_turn(partial_text):
        return min(250, base_silence_ms)
    if looks_semantically_incomplete(partial_text):
        return max(1200, base_silence_ms + 500)
    return base_silence_ms


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
