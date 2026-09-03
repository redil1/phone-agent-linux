"""Kokoro-compatible eSpeak G2P through an isolated executable boundary.

The release image does not import, link, or bundle an eSpeak Python library. It invokes the
distribution-provided GPL program as a separate process and consumes only its phoneme output.
"""

# IPA symbols are intentional data, not confusable source identifiers.
# ruff: noqa: RUF001

from __future__ import annotations

import functools
import re
import subprocess

_E2M = tuple(
    sorted(
        {
            "ʔˌn\u0329": "ʔn",
            "ʔn\u0329": "ʔn",
            "a^ɪ": "I",
            "a^ʊ": "W",
            "d^ʒ": "ʤ",
            "e^ɪ": "A",
            "e": "A",
            "t^ʃ": "ʧ",
            "ɔ^ɪ": "Y",
            "ə^l": "ᵊl",
            "ʲo": "jo",
            "ʲə": "jə",
            "ʲ": "",
            "ɚ": "əɹ",
            "r": "ɹ",
            "x": "k",
            "ç": "k",
            "ɐ": "ə",
            "ɬ": "l",
            "\u0303": "",
        }.items(),
        key=lambda item: -len(item[0]),
    )
)


@functools.lru_cache(maxsize=4096)
def _phonemize(text: str, language: str) -> str:
    if not text.strip():
        return ""
    result = subprocess.run(
        ["espeak-ng", "-q", "--ipa=3", "-v", language, text],
        check=True,
        capture_output=True,
        text=True,
        timeout=2.0,
    )
    return " ".join(result.stdout.split()).replace("\u200d", "^")


def _normalize_english(phonemes: str, *, british: bool, version: str | None) -> str:
    for old, new in _E2M:
        phonemes = phonemes.replace(old, new)
    phonemes = re.sub(r"(\S)\u0329", r"ᵊ\1", phonemes).replace(chr(809), "")
    if british:
        phonemes = phonemes.replace("e^ə", "ɛː")
        phonemes = phonemes.replace("iə", "ɪə")
        phonemes = phonemes.replace("ə^ʊ", "Q")
    else:
        phonemes = phonemes.replace("o^ʊ", "O")
        phonemes = phonemes.replace("ɜːɹ", "ɜɹ")
        phonemes = phonemes.replace("ɜː", "ɜɹ")
        phonemes = phonemes.replace("ɪə", "iə")
        phonemes = phonemes.replace("ː", "")
    phonemes = phonemes.replace("o", "ɔ")
    if version != "2.0":
        phonemes = phonemes.replace("ɾ", "T").replace("ʔ", "t")
    return phonemes.replace("^", "")


class EspeakFallback:
    """Last-resort pronunciation for English words outside Misaki's dictionaries."""

    def __init__(self, british: bool, version: str | None = None) -> None:
        self.british = british
        self.version = version
        _phonemize("ready", "en-gb" if british else "en-us")

    def __call__(self, token: object) -> tuple[str | None, int | None]:
        text = str(getattr(token, "text", "")).strip()
        if not text:
            return None, None
        phonemes = _phonemize(text, "en-gb" if self.british else "en-us")
        if not phonemes:
            return None, None
        return _normalize_english(
            phonemes,
            british=self.british,
            version=self.version,
        ), 2


class EspeakG2P:
    """Whole-text G2P for the non-English languages supported by Kokoro 1.0."""

    def __init__(self, language: str, version: str | None = None) -> None:
        self.language = language
        self.version = version
        _phonemize("ready", language)

    def __call__(self, text: str) -> tuple[str, None]:
        phonemes = _phonemize(text.replace("(", "«").replace(")", "»"), self.language)
        replacements = {
            "a^ɪ": "I",
            "a^ʊ": "W",
            "d^z": "ʣ",
            "d^ʒ": "ʤ",
            "e^ɪ": "A",
            "o^ʊ": "O",
            "ə^ʊ": "Q",
            "s^s": "S",
            "t^s": "ʦ",
            "t^ʃ": "ʧ",
            "ɔ^ɪ": "Y",
        }
        for old, new in replacements.items():
            phonemes = phonemes.replace(old, new)
        phonemes = phonemes.replace("^", "")
        if self.version == "2.0":
            phonemes = phonemes.replace(chr(809), "").replace(chr(810), "")
            phonemes = re.sub(r"(\S)\u0329", r"ᵊ\1", phonemes)
        else:
            phonemes = phonemes.replace("-", "")
        return phonemes.replace("«", "(").replace("»", ")"), None
