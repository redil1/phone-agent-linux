"""Streaming TTS, normalization, and verified speech delivery.

Governed by Milestone 8 (M8-01 through M8-12):
Standardizes TTS contract, universal text normalization, pronunciation dictionaries,
distinct delivery states, and audible-output supervision.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UniversalTextNormalizer:
    """Universal text normalizer for telephony TTS (M8-03, M8-04)."""

    def __init__(self, dictionary: dict[str, str] | None = None) -> None:
        self.dictionary = dictionary or {
            "IPTV": "I P T V",
            "OXzoon": "Ox zoon",
            "USD": "dollars",
            "EUR": "euros",
        }

    def normalize(self, text: str) -> str:
        res = text
        # Expand currency symbols
        res = re.sub(r"\$(\d+)", r"\1 dollars", res)
        res = re.sub(r"€(\d+)", r"\1 euros", res)

        # Dictionary replacement
        for k, v in self.dictionary.items():
            res = re.sub(rf"\b{re.escape(k)}\b", v, res)
        return res


class SpeechDeliverySupervisor:
    """Tracks distinct synthesis and playout delivery states (M8-09, M8-10)."""

    def __init__(self) -> None:
        self.state: Literal[
            "idle", "generated", "queued", "sent", "rendered", "interrupted", "failed"
        ] = "idle"
        self.delivered_characters: int = 0

    def on_synthesis_generated(self) -> None:
        self.state = "generated"

    def on_queued(self) -> None:
        self.state = "queued"

    def on_sent(self) -> None:
        self.state = "sent"

    def on_rendered(self, char_count: int) -> None:
        self.state = "rendered"
        self.delivered_characters += char_count

    def on_interrupted(self) -> None:
        self.state = "interrupted"
