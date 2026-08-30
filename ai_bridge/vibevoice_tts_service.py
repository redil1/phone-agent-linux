"""Experimental local VibeVoice-Realtime speech synthesis.

This backend is deliberately not a production default. Microsoft documents the
model as "intended for research and development purposes only" and does not
recommend it for commercial use without further testing, and it names French as
an exploration language whose transcripts "may result in unexpected audio
outputs". A single local measurement on an M4 Max also produced a real-time
factor above 1.0, meaning it can generate speech no faster than the phone plays
it, which leaves no margin for the starvation the Android playout queue already
counts.

It is exposed so the trade-off can be measured on a real call rather than
argued about. Prefer ``supertonic`` for production French and English.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import AsyncGenerator
from typing import Any

import numpy as np
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.tts_service import TextAggregationMode, TTSService
from pipecat.transcriptions.language import Language

from .supertonic_tts_service import _waveform_to_pcm16

logger = logging.getLogger("PhoneAgentVibeVoiceTTS")

DEFAULT_MODEL = "mlx-community/VibeVoice-Realtime-0.5B-8bit"
DEFAULT_VOICE = "en-Emma_woman"
NATIVE_SAMPLE_RATE = 24_000
_SPEAKABLE_RE = re.compile(r"[\w\d]", re.UNICODE)
VOICE_RE = re.compile(r"^[a-z]{2}-[A-Za-z0-9_]+$")

# Only these ship with the released voice cache. English is the model's
# supported language; the rest are Microsoft's "exploration" set.
KNOWN_VOICES = (
    "en-Carter_man",
    "en-Davis_man",
    "en-Emma_woman",
    "en-Frank_man",
    "en-Grace_woman",
    "en-Mike_man",
    "fr-Spk0_man",
    "fr-Spk1_woman",
    "de-Spk0_man",
    "de-Spk1_woman",
    "it-Spk0_woman",
    "it-Spk1_man",
    "sp-Spk0_woman",
    "sp-Spk1_man",
    "pt-Spk0_woman",
    "pt-Spk1_man",
    "nl-Spk0_man",
    "nl-Spk1_woman",
    "pl-Spk0_woman",
    "pl-Spk1_man",
    "jp-Spk0_man",
    "jp-Spk1_woman",
    "kr-Spk0_woman",
    "kr-Spk1_man",
    "in-Samuel_man",
)

# One process-wide model and one inference lock. Weights are large and the GPU
# is shared with local STT, so overlapping passes would stall both.
_MODEL_CACHE: dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()


def default_voice_for_language(language: str) -> str:
    return "fr-Spk0_man" if language.lower().startswith("fr") else DEFAULT_VOICE


def load_model(model_id: str = DEFAULT_MODEL) -> Any:
    """Load and cache the model. The first call can take minutes."""

    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(model_id)
        if cached is not None:
            return cached
        from mlx_audio.tts.utils import load_model as _load

        started = time.perf_counter()
        model = _load(model_id)
        _MODEL_CACHE[model_id] = model
        logger.info(
            "loaded VibeVoice model=%s elapsed_ms=%.1f",
            model_id,
            (time.perf_counter() - started) * 1000,
        )
        return model


def synthesize_pcm(
    text: str,
    *,
    model_id: str = DEFAULT_MODEL,
    voice: str = DEFAULT_VOICE,
    target_sample_rate: int = 16_000,
    ddpm_steps: int = 10,
    cfg_scale: float = 1.3,
) -> bytes:
    """Render one utterance to phone-ready 16 kHz mono PCM16."""

    model = load_model(model_id)
    chunks: list[np.ndarray] = []
    source_rate = NATIVE_SAMPLE_RATE
    with _INFERENCE_LOCK:
        for segment in model.generate(
            text=text, voice=voice, ddpm_steps=ddpm_steps, cfg_scale=cfg_scale
        ):
            audio = np.asarray(segment.audio, dtype=np.float32).squeeze()
            if audio.size:
                chunks.append(audio)
            source_rate = int(getattr(segment, "sample_rate", NATIVE_SAMPLE_RATE) or source_rate)
    if not chunks:
        return b""
    return _waveform_to_pcm16(np.concatenate(chunks), source_rate, target_sample_rate)


def prewarm_vibevoice(
    *,
    model_id: str = DEFAULT_MODEL,
    voice: str = DEFAULT_VOICE,
    sample_rate: int = 16_000,
) -> float:
    """Load weights and run one short utterance before the first caller turn."""

    started = time.perf_counter()
    synthesize_pcm("Bonjour.", model_id=model_id, voice=voice, target_sample_rate=sample_rate)
    return (time.perf_counter() - started) * 1000


class VibeVoiceTTSService(TTSService):
    """Pipecat adapter for local VibeVoice-Realtime synthesis."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        language: str = "en-US",
        ddpm_steps: int = 10,
        cfg_scale: float = 1.3,
        text_aggregation_mode: TextAggregationMode = TextAggregationMode.SENTENCE,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            sample_rate=sample_rate,
            text_aggregation_mode=text_aggregation_mode,
            **kwargs,
        )
        if not VOICE_RE.fullmatch(voice):
            raise ValueError(f"unsupported VibeVoice voice: {voice!r}")
        self._model_id = model
        self._voice = voice
        self._language = language
        self._ddpm_steps = ddpm_steps
        self._cfg_scale = cfg_scale
        self._target_sample_rate = sample_rate
        self._frame_bytes = sample_rate * 2 * 20 // 1000
        if not language.lower().startswith("en"):
            logger.warning(
                "VibeVoice is documented as English-only; %s is an exploration language "
                "and may produce unexpected audio",
                language,
            )

    def can_generate_metrics(self) -> bool:
        return True

    def language_to_service_language(self, language: Language) -> str:
        return str(language)

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        phrase = text.strip()
        if not phrase or not _SPEAKABLE_RE.search(phrase):
            return
        emitted = False
        try:
            await self.start_tts_usage_metrics(phrase)
            pcm = await asyncio.to_thread(
                synthesize_pcm,
                phrase,
                model_id=self._model_id,
                voice=self._voice,
                target_sample_rate=self._target_sample_rate,
                ddpm_steps=self._ddpm_steps,
                cfg_scale=self._cfg_scale,
            )
            for offset in range(0, len(pcm), self._frame_bytes):
                chunk = pcm[offset : offset + self._frame_bytes]
                if not chunk:
                    continue
                if not emitted:
                    await self.stop_ttfb_metrics()
                    emitted = True
                yield TTSAudioRawFrame(
                    audio=chunk,
                    sample_rate=self._target_sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )
            if not emitted:
                yield ErrorFrame(error="VibeVoice completed without playable audio")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("VibeVoice synthesis failed")
            yield ErrorFrame(error=f"VibeVoice TTS error: {exc}")
        finally:
            await self.stop_ttfb_metrics()
