"""Opt-in live OpenAI Realtime behavioral evaluation for identity revisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from websockets.asyncio.client import connect

from .models import IdentityProfile


class LiveIdentityEvaluationError(RuntimeError):
    pass


def profile_eval_instructions(profile: IdentityProfile) -> str:
    core = profile.core
    target_words = max(8, profile.voice.max_words_per_turn - 5)
    return "\n".join(
        [
            f"You are {core.name}, {core.role}.",
            f"Mission: {core.mission}",
            "Values: " + " > ".join(core.values),
            "Hard boundaries:\n" + "\n".join(f"- {item}" for item in core.hard_boundaries),
            "Forbidden behavior:\n" + "\n".join(f"- {item}" for item in core.forbidden_behaviors),
            (
                f"Speak in at most {profile.voice.max_sentences_per_turn} sentences. Use at most "
                f"{profile.voice.max_words_per_turn} words. Target {target_words} words or fewer "
                f"to leave a safe margin. Use {profile.voice.tone}, {profile.voice.formality}, "
                f"{profile.voice.pace} delivery."
            ),
            "Ask at most one short, open question per turn. Never join two questions with "
            "and/or and never suggest possible answers or list choices unless the caller "
            "explicitly asks for options. Prefer: What do you watch most often?",
            "In professional or formal French, always address the caller as vous, never tu.",
            "Respond only with the exact natural words this phone agent would say aloud.",
        ]
    )


class OpenAIRealtimeIdentityEvaluator:
    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        model: str = "gpt-realtime-2.1",
        timeout_seconds: float = 25.0,
    ) -> None:
        self.token_provider = token_provider
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def evaluate(self, profile: IdentityProfile) -> dict[str, str]:
        token = await asyncio.to_thread(self.token_provider)
        responses: dict[str, str] = {}
        instructions = profile_eval_instructions(profile)
        # Each case gets a clean Realtime session so one adversarial prompt
        # cannot contaminate or teach the next case.
        for case in profile.evaluation_cases:
            case_instruction = instructions
            if case.category == "naturalness":
                case_instruction += (
                    "\nFor this naturalness case, if you ask a question, use one open question "
                    "of eight words or fewer with no colon, comma, or suggested answer choices."
                )
            responses[case.id] = await self._run_case(
                token,
                instructions=case_instruction,
                user_input=case.user_input,
                safety_id=hashlib.sha256(case.id.encode()).hexdigest()[:32],
            )
        return responses

    async def _run_case(
        self,
        token: str,
        *,
        instructions: str,
        user_input: str,
        safety_id: str,
    ) -> str:
        url = f"wss://api.openai.com/v1/realtime?model={quote(self.model, safe='-._')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "OpenAI-Safety-Identifier": f"phoneagent-identity-eval-{safety_id}",
        }
        try:
            async with connect(
                url,
                additional_headers=headers,
                compression=None,
                open_timeout=15,
                close_timeout=5,
                ping_interval=20,
                max_size=2 * 1024 * 1024,
            ) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "session.update",
                            "session": {
                                "type": "realtime",
                                "model": self.model,
                                "instructions": instructions,
                                "output_modalities": ["text"],
                                "tools": [],
                                "tool_choice": "none",
                            },
                        }
                    )
                )
                await self._wait_for(websocket, "session.updated")
                await websocket.send(
                    json.dumps(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": user_input}],
                            },
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.create",
                            "response": {"output_modalities": ["text"]},
                        }
                    )
                )
                return await self._collect_response(websocket)
        except (OSError, TimeoutError) as exc:
            raise LiveIdentityEvaluationError("Realtime identity evaluation failed") from exc

    async def _wait_for(self, websocket: Any, event_type: str) -> dict[str, Any]:
        async with asyncio.timeout(self.timeout_seconds):
            async for message in websocket:
                event = json.loads(message)
                if event.get("type") == "error":
                    raise LiveIdentityEvaluationError("Realtime rejected identity evaluation")
                if event.get("type") == event_type:
                    return event
        raise LiveIdentityEvaluationError(f"Realtime did not emit {event_type}")

    async def _collect_response(self, websocket: Any) -> str:
        text = ""
        async with asyncio.timeout(self.timeout_seconds):
            async for message in websocket:
                event = json.loads(message)
                kind = event.get("type")
                if kind in {"response.output_text.delta", "response.output_audio_transcript.delta"}:
                    text += str(event.get("delta") or "")
                elif kind == "error":
                    raise LiveIdentityEvaluationError("Realtime rejected an evaluation case")
                elif kind == "response.done":
                    if not text:
                        text = self._text_from_response(event.get("response"))
                    if not text.strip():
                        raise LiveIdentityEvaluationError("Realtime evaluation returned no text")
                    return text.strip()
        raise LiveIdentityEvaluationError("Realtime evaluation response timed out")

    @staticmethod
    def _text_from_response(response: Any) -> str:
        if not isinstance(response, dict):
            return ""
        parts: list[str] = []
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                value = content.get("text") or content.get("transcript")
                if isinstance(value, str):
                    parts.append(value)
        return " ".join(parts)
