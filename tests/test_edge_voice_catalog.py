"""Tests for the dynamic English/French Edge voice catalog."""

from __future__ import annotations

import asyncio
from typing import Any

from phone_agent_gateway.ai_bridge.edge_voice_catalog import (
    fallback_edge_voice_catalog,
    fetch_edge_voice_catalog,
)


def test_dynamic_catalog_filters_and_normalizes_supported_locales() -> None:
    async def list_voices() -> list[dict[str, Any]]:
        return [
            {
                "ShortName": "fr-CA-JeanNeural",
                "Locale": "fr-CA",
                "Gender": "Male",
                "FriendlyName": "Microsoft Jean",
                "Status": "GA",
            },
            {
                "ShortName": "en-GB-RyanNeural",
                "Locale": "en-GB",
                "Gender": "Male",
            },
            {
                "ShortName": "en-US-AndrewMultilingualNeural",
                "Locale": "en-US",
                "Gender": "Male",
                "FriendlyName": "Microsoft AndrewMultilingual",
                "Status": "GA",
            },
            {
                "ShortName": "fr-FR-DeniseNeural",
                "Locale": "fr-FR",
                "Gender": "Female",
                "FriendlyName": "Microsoft Denise",
                "Status": "GA",
            },
        ]

    catalog = asyncio.run(fetch_edge_voice_catalog(list_voices))

    assert [voice["short_name"] for voice in catalog] == [
        "en-US-AndrewMultilingualNeural",
        "fr-FR-DeniseNeural",
        "fr-CA-JeanNeural",
    ]
    assert catalog[0]["display_name"] == "Andrew — Multilingual"
    assert catalog[0]["multilingual"] is True


def test_fallback_catalog_keeps_complete_current_selection() -> None:
    catalog = fallback_edge_voice_catalog()
    names = {voice["short_name"] for voice in catalog}

    assert len(catalog) == 30
    assert "en-US-AndrewMultilingualNeural" in names
    assert "fr-FR-RemyMultilingualNeural" in names
    assert {voice["locale"] for voice in catalog} == {
        "en-US",
        "fr-FR",
        "fr-CA",
        "fr-BE",
        "fr-CH",
    }
