"""Context-safe conversational reactions that hide remote provider latency."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import Callable
from typing import Any

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

EventSink = Callable[[dict[str, Any]], Any]

_REFLEX_PHRASES = {
    "en": "I see.",
    "fr": "Je vois.",
}
_FRENCH_SIGNAL_RE = re.compile(
    r"\b(?:bonjour|merci|pourquoi|pouvez|peux|besoin|voudrais|quand|quoi|avec|"
    r"comment\s+allez|est-ce|qu['\u2019]est|j['\u2019]ai|c['\u2019]est)\b",
    re.IGNORECASE,
)
_ACKNOWLEDGEMENT_ONLY = {
    "hello",
    "hi",
    "yes",
    "no",
    "okay",
    "ok",
    "thanks",
    "thank you",
    "goodbye",
    "bye",
    "bonjour",
    "salut",
    "oui",
    "non",
    "d accord",
    "merci",
    "au revoir",
}


def reflex_phrases() -> tuple[str, ...]:
    """Return the small immutable phrase set that can be warmed at call start."""

    return tuple(_REFLEX_PHRASES.values())


def select_reflex(text: str, configured_language: str) -> str | None:
    """Choose a universally safe reaction, or suppress it for trivial turns."""

    normalized = " ".join(text.split()).strip()
    if not normalized:
        return None
    words = re.findall(r"[^\W_]+(?:['\u2019][^\W_]+)?", normalized, re.UNICODE)
    plain = re.sub(r"[^\w\s]", " ", normalized.lower(), flags=re.UNICODE)
    plain = " ".join(plain.replace("_", " ").split())
    if len(words) < 4 or plain in _ACKNOWLEDGEMENT_ONLY:
        return None

    language = "fr" if configured_language.lower().startswith("fr") else "en"
    if _FRENCH_SIGNAL_RE.search(normalized):
        language = "fr"
    return _REFLEX_PHRASES[language]


class ConversationalReflexProcessor(FrameProcessor):
    """Play cached non-context speech immediately before confirmed LLM work.

    The processor sits after the user context aggregator.  It therefore sees
    only an authoritative ``LLMContextFrame`` and never guesses whether the
    caller has finished.  The original frame is passed through untouched.
    """

    def __init__(
        self,
        *,
        tts: Any,
        language: str,
        enabled: bool,
        sample_rate: int = 16_000,
        cooldown_ms: int = 8000,
        event_sink: EventSink | None = None,
    ) -> None:
        super().__init__()
        self._tts = tts
        self._language = language
        self._enabled = enabled
        self._sample_rate = sample_rate
        self._cooldown_sec = cooldown_ms / 1000.0
        self._event_sink = event_sink
        self._last_played_at = 0.0
        self._sequence = 0
        self._warmup_task: asyncio.Task[None] | None = None

    @property
    def supported(self) -> bool:
        return callable(getattr(self._tts, "warm_reflexes", None)) and callable(
            getattr(self._tts, "get_reflex_pcm", None)
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame) and self._enabled and self.supported:
            self._start_warmup()

        if (
            direction is FrameDirection.DOWNSTREAM
            and isinstance(frame, LLMContextFrame)
            and self._enabled
            and self.supported
        ):
            await self._maybe_play(frame)

        await self.push_frame(frame, direction)

    def _start_warmup(self) -> None:
        if self._warmup_task is not None and not self._warmup_task.done():
            return

        async def warm() -> None:
            try:
                await self._tts.warm_reflexes(reflex_phrases())
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._emit("warmup_failed")

        self._warmup_task = asyncio.create_task(warm(), name="phoneagent-reflex-warmup")

    async def _maybe_play(self, frame: LLMContextFrame) -> None:
        messages = frame.context.get_messages()
        caller_text = ""
        for message in reversed(messages):
            if isinstance(message, dict) and str(message.get("role", "")).lower() == "user":
                caller_text = str(message.get("content", "")).strip()
                break
        phrase = select_reflex(caller_text, self._language)
        if phrase is None:
            await self._emit("suppressed")
            return

        now = time.monotonic()
        if self._last_played_at and now - self._last_played_at < self._cooldown_sec:
            await self._emit("cooldown")
            return

        has_ready_speculation = getattr(self._tts, "has_ready_speculative_audio", None)
        if callable(has_ready_speculation) and has_ready_speculation():
            await self._emit("substantive_audio_ready")
            return

        pcm = self._tts.get_reflex_pcm(phrase)
        if not pcm:
            self._start_warmup()
            await self._emit("cache_miss")
            return

        self._sequence += 1
        context_id = f"reflex-{self._sequence}"
        await self.push_frame(
            TTSStartedFrame(context_id=context_id, append_to_context=False),
            FrameDirection.DOWNSTREAM,
        )
        for offset in range(0, len(pcm), 4096):
            await self.push_frame(
                TTSAudioRawFrame(
                    audio=pcm[offset : offset + 4096],
                    sample_rate=self._sample_rate,
                    num_channels=1,
                    context_id=context_id,
                ),
                FrameDirection.DOWNSTREAM,
            )
        await self.push_frame(
            TTSStoppedFrame(context_id=context_id),
            FrameDirection.DOWNSTREAM,
        )
        self._last_played_at = now
        await self._emit(
            "played",
            phrase=phrase,
            audio_ms=round(len(pcm) / (self._sample_rate * 2) * 1000, 1),
        )

    async def cleanup(self) -> None:
        task = self._warmup_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._warmup_task = None
        await super().cleanup()

    async def _emit(self, state: str, **details: object) -> None:
        if self._event_sink is None:
            return
        event: dict[str, object] = {"type": "conversational_reflex", "state": state}
        event.update(details)
        result = self._event_sink(event)
        if inspect.isawaitable(result):
            await result
