"""Current Edge TTS voice catalog for the English/French Studio selector."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import edge_tts

VoiceLister = Callable[[], Awaitable[list[dict[str, Any]]]]

SUPPORTED_EDGE_LOCALES = ("en-US", "fr-FR", "fr-CA", "fr-BE", "fr-CH")
_LOCALE_ORDER = {locale: index for index, locale in enumerate(SUPPORTED_EDGE_LOCALES)}

# Used only when Microsoft's catalog endpoint is temporarily unavailable. The
# normal Studio path always loads the current list dynamically via edge-tts.
FALLBACK_EDGE_VOICES: tuple[tuple[str, str, str], ...] = (
    ("en-US-AnaNeural", "en-US", "Female"),
    ("en-US-AriaNeural", "en-US", "Female"),
    ("en-US-AvaMultilingualNeural", "en-US", "Female"),
    ("en-US-AvaNeural", "en-US", "Female"),
    ("en-US-EmmaMultilingualNeural", "en-US", "Female"),
    ("en-US-EmmaNeural", "en-US", "Female"),
    ("en-US-JennyNeural", "en-US", "Female"),
    ("en-US-MichelleNeural", "en-US", "Female"),
    ("en-US-AndrewMultilingualNeural", "en-US", "Male"),
    ("en-US-AndrewNeural", "en-US", "Male"),
    ("en-US-BrianMultilingualNeural", "en-US", "Male"),
    ("en-US-BrianNeural", "en-US", "Male"),
    ("en-US-ChristopherNeural", "en-US", "Male"),
    ("en-US-EricNeural", "en-US", "Male"),
    ("en-US-GuyNeural", "en-US", "Male"),
    ("en-US-RogerNeural", "en-US", "Male"),
    ("en-US-SteffanNeural", "en-US", "Male"),
    ("fr-FR-DeniseNeural", "fr-FR", "Female"),
    ("fr-FR-EloiseNeural", "fr-FR", "Female"),
    ("fr-FR-VivienneMultilingualNeural", "fr-FR", "Female"),
    ("fr-FR-HenriNeural", "fr-FR", "Male"),
    ("fr-FR-RemyMultilingualNeural", "fr-FR", "Male"),
    ("fr-CA-SylvieNeural", "fr-CA", "Female"),
    ("fr-CA-AntoineNeural", "fr-CA", "Male"),
    ("fr-CA-JeanNeural", "fr-CA", "Male"),
    ("fr-CA-ThierryNeural", "fr-CA", "Male"),
    ("fr-BE-CharlineNeural", "fr-BE", "Female"),
    ("fr-BE-GerardNeural", "fr-BE", "Male"),
    ("fr-CH-ArianeNeural", "fr-CH", "Female"),
    ("fr-CH-FabriceNeural", "fr-CH", "Male"),
)


def _display_name(short_name: str) -> str:
    name = short_name.split("-", 2)[-1]
    if name.endswith("MultilingualNeural"):
        return name.removesuffix("MultilingualNeural") + " — Multilingual"
    return name.removesuffix("Neural")


def _normalized_voice(
    *,
    short_name: str,
    locale: str,
    gender: str,
    friendly_name: str = "",
    status: str = "GA",
) -> dict[str, object]:
    return {
        "short_name": short_name,
        "locale": locale,
        "gender": gender,
        "display_name": _display_name(short_name),
        "friendly_name": friendly_name,
        "multilingual": "Multilingual" in short_name,
        "status": status,
    }


def fallback_edge_voice_catalog() -> list[dict[str, object]]:
    return [
        _normalized_voice(short_name=name, locale=locale, gender=gender)
        for name, locale, gender in FALLBACK_EDGE_VOICES
    ]


async def fetch_edge_voice_catalog(
    list_voices: VoiceLister | None = None,
) -> list[dict[str, object]]:
    """Fetch and normalize every current en-US and French Edge voice."""

    lister = list_voices or edge_tts.list_voices
    raw_voices = await lister()
    catalog = [
        _normalized_voice(
            short_name=str(voice.get("ShortName", "")),
            locale=str(voice.get("Locale", "")),
            gender=str(voice.get("Gender", "")),
            friendly_name=str(voice.get("FriendlyName", "")),
            status=str(voice.get("Status", "")),
        )
        for voice in raw_voices
        if voice.get("Locale") in SUPPORTED_EDGE_LOCALES and voice.get("ShortName")
    ]
    catalog.sort(
        key=lambda voice: (
            _LOCALE_ORDER.get(str(voice["locale"]), len(_LOCALE_ORDER)),
            0 if voice["gender"] == "Female" else 1,
            str(voice["display_name"]),
        )
    )
    if not catalog:
        raise RuntimeError("Edge returned no supported English/French voices")
    return catalog
