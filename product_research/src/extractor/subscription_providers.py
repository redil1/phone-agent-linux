"""Extraction backends that reuse PhoneAgent's existing local logins.

Both providers here need no API key: they drive a session the operator has
already authenticated on this machine.

* ``codex``       - the Codex app-server bundled with ChatGPT, on the user's
                    own ChatGPT plan. The strongest option available offline
                    from a subscription, and the only one that reaches
                    GPT-5-class models here.
* ``antigravity`` - the local Antigravity language-server bridge, on the
                    user's Google account, serving Gemini 3.7 Flash.

The engine runs as a subprocess of the PhoneAgent virtual environment, so these
import from the installed gateway package rather than duplicating its clients.
"""

from __future__ import annotations

import json
import re
from typing import Any

# A seven-pillar extraction is one large completion, not a conversational turn.
CODEX_TIMEOUT_SECS = 600.0
ANTIGRAVITY_TIMEOUT_SECS = 300.0
CODEX_DEFAULT_MODEL = "gpt-5.4-mini"
# Probed against a live Antigravity bridge: every 3.7 variant answers, while
# gemini-3-flash 404s and gemini-2.5-pro 503s. The tiered variant lets the
# bridge pick its own effort per request, which suits a single very large
# extraction better than pinning one fixed tier.
ANTIGRAVITY_DEFAULT_MODEL = "gemini-3.7-flash-tiered"
ANTIGRAVITY_MODELS = [
    "gemini-3.7-flash-tiered",
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-low",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-thinking",
    "gemini-2.5-flash-lite",
]

_EXTRACTION_INSTRUCTIONS = (
    "You extract structured product data and return JSON. Do not call tools, "
    "inspect files, run commands, or modify the computer. Return only the JSON "
    "object asked for, with no prose and no Markdown fences."
)


def available_subscription_providers() -> dict[str, list[str]]:
    """Which of these are usable right now, and with which models.

    Checked cheaply: a binary on disk and an open bridge port. A provider that
    is present but not signed in surfaces its own error at extraction time.
    """

    providers: dict[str, list[str]] = {}

    try:
        from phone_agent_gateway.ai_bridge.codex_app_server import resolve_codex_binary

        resolve_codex_binary()
        providers["codex"] = [
            CODEX_DEFAULT_MODEL, "gpt-5.4", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-luna",
        ]
    except Exception:
        pass

    try:
        import socket

        for port in range(53850, 53872):
            with socket.socket() as probe:
                probe.settimeout(0.05)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    providers["antigravity"] = list(ANTIGRAVITY_MODELS)
                    break
    except Exception:
        pass

    return providers


def _parse_json(raw: str, provider: str) -> dict[str, Any]:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{provider} did not return parseable JSON ({exc}). First 200 characters: "
            f"{(raw or '')[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{provider} returned {type(parsed).__name__}, expected a JSON object")
    return parsed


async def call_codex(system_prompt: str, user_prompt: str, model: str | None) -> dict[str, Any]:
    """Extract using the ChatGPT subscription, via the local Codex app-server."""

    from phone_agent_gateway.ai_bridge.codex_app_server import CodexAppServerClient

    client = CodexAppServerClient(request_timeout_secs=CODEX_TIMEOUT_SECS)
    chunks: list[str] = []
    try:
        await client.start()
        thread_id = await client.start_thread(
            model=model or CODEX_DEFAULT_MODEL,
            system_instruction=system_prompt,
            developer_instructions=_EXTRACTION_INSTRUCTIONS,
        )
        async for chunk in client.stream_turn(
            thread_id, user_prompt, effort="low", timeout_secs=CODEX_TIMEOUT_SECS
        ):
            chunks.append(chunk)
    finally:
        try:
            await client.close()
        except Exception:
            pass
    return _parse_json("".join(chunks), "Codex")


async def call_antigravity(
    system_prompt: str, user_prompt: str, model: str | None
) -> dict[str, Any]:
    """Extract using the Google account already signed in to Antigravity."""

    import aiohttp
    from phone_agent_gateway.ai_bridge.antigravity_gemini_llm import (
        MODEL_MAP,
        AntigravityGeminiLLMService,
    )

    service = AntigravityGeminiLLMService.__new__(AntigravityGeminiLLMService)
    service._base_url = None
    service._csrf_token = None
    service._session = None
    service._ssl_ctx = AntigravityGeminiLLMService._create_ssl_context(service)
    chosen = model or ANTIGRAVITY_DEFAULT_MODEL
    service._enum_model = MODEL_MAP.get(chosen, chosen)
    service._turn_timeout_secs = ANTIGRAVITY_TIMEOUT_SECS

    if not await service._discover_bridge():
        raise RuntimeError(
            "The Antigravity language-server bridge is not running. Open Antigravity and "
            "sign in, or choose another extraction provider."
        )
    service._session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=service._ssl_ctx)
    )
    try:
        raw = await service._generate_gemini(
            f"{system_prompt}\n\n{_EXTRACTION_INSTRUCTIONS}\n\n{user_prompt}"
        )
    finally:
        await service._session.close()
    return _parse_json(str(raw), "Antigravity")
