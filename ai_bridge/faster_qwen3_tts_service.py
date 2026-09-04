"""FasterQwen3TTS Service for Pipecat Telephony.

Provides hyper-fast streaming TTS using faster-qwen3-tts with CUDA graph capture
on both 0.6B and 1.7B parameter models (CustomVoice and VoiceClone).
Resamples 24 kHz audio to 16 kHz signed 16-bit mono PCM for telephony streaming.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import soxr
import torch
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts

logger = logging.getLogger("FasterQwen3TTS")
LatencySink = Callable[[dict[str, Any]], Awaitable[None] | None]

QWEN3_SAMPLE_RATE = 24_000
DEFAULT_06B_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
DEFAULT_17B_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
DEFAULT_SPEAKER = "ryan"

VALID_SPEAKERS = {
    "ryan", "aiden", "dylan", "eric", "ono_anna", "serena", "sohee", "uncle_fu", "vivian"
}


@dataclass
class _Qwen3Engine:
    model: Any
    executor: ThreadPoolExecutor
    device: str
    model_id: str


_ENGINE_CACHE: dict[str, _Qwen3Engine] = {}
_ENGINE_LOCK = threading.Lock()


def load_faster_qwen3_model(
    model_id: str = DEFAULT_06B_MODEL,
    device: str = "cuda",
) -> _Qwen3Engine:
    with _ENGINE_LOCK:
        if model_id in _ENGINE_CACHE:
            return _ENGINE_CACHE[model_id]

        from faster_qwen3_tts import FasterQwen3TTS

        logger.info("Loading FasterQwen3TTS model=%s on device=%s", model_id, device)
        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        model = FasterQwen3TTS.from_pretrained(
            model_id,
            device=device,
            dtype=dtype,
            attn_implementation="sdpa",
            max_seq_len=2048,
        )
        logger.info("Running CUDA graph warmup for %s...", model_id)
        model.warmup()
        # Prime speech generation with dummy sentence so all CUDA streaming kernels are resident in VRAM
        try:
            logger.info("Priming live streaming speech generation for zero-latency TTFA...")
            for _c, _sr, _ in model.generate_custom_voice_streaming(
                text="Hello",
                speaker=DEFAULT_SPEAKER,
                language="english",
                chunk_size=4
            ):
                pass
            logger.info("FasterQwen3TTS permanently warm and locked in VRAM!")
        except Exception as prime_err:
            logger.warning("Initial dummy generation error: %s", prime_err)

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="FasterQwen3TTS")
        engine = _Qwen3Engine(
            model=model,
            executor=executor,
            device=device,
            model_id=model_id,
        )
        _ENGINE_CACHE[model_id] = engine
        return engine


def prewarm_faster_qwen3(model_id: str = DEFAULT_06B_MODEL) -> float:
    started = time.perf_counter()
    load_faster_qwen3_model(model_id, device="cuda")
    return (time.perf_counter() - started) * 1000


@dataclass
class FasterQwen3Settings(TTSSettings):
    """Runtime settings for FasterQwen3TTS model."""
    voice: str = DEFAULT_SPEAKER
    speed: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)


class FasterQwen3TTSService(TTSService):
    """Pipecat TTS service backed by FasterQwen3TTS on CUDA with streaming."""

    Settings = FasterQwen3Settings
    _settings: FasterQwen3Settings

    def __init__(
        self,
        *,
        model: str = DEFAULT_06B_MODEL,
        voice: str = DEFAULT_SPEAKER,
        sample_rate: int = 16_000,
        language: str = "english",
        chunk_size: int = 4,
        latency_sink: LatencySink | None = None,
        **kwargs: Any,
    ) -> None:
        resolved_voice = voice if voice in VALID_SPEAKERS else DEFAULT_SPEAKER
        settings = self.Settings(
            model=model,
            voice=resolved_voice,
            language=None,
        )
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            settings=settings,
            **kwargs,
        )
        self._model_id = model
        self._voice = resolved_voice
        self._target_sample_rate = sample_rate
        self._language = language.lower()
        self._chunk_size = chunk_size
        self._latency_sink = latency_sink
        self._engine: _Qwen3Engine | None = None

    def set_latency_sink(self, sink: LatencySink | None) -> None:
        self._latency_sink = sink

    def _ensure_engine(self) -> _Qwen3Engine:
        if self._engine is None:
            self._engine = load_faster_qwen3_model(self._model_id)
        return self._engine

    def can_generate_metrics(self) -> bool:
        return True

    def _generate_audio_chunks(
        self, text: str, voice: str, language: str
    ) -> list[np.ndarray]:
        engine = self._ensure_engine()
        chunks: list[np.ndarray] = []
        for chunk, _sr, _ in engine.model.generate_custom_voice_streaming(
            text=text,
            speaker=voice,
            language=language,
            chunk_size=self._chunk_size,
        ):
            if chunk is not None and len(chunk) > 0:
                chunks.append(chunk)
        return chunks

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        clean_text = " ".join(text.strip().split())
        if not clean_text:
            return

        engine = self._ensure_engine()
        voice = getattr(self._settings, "voice", self._voice) or self._voice or DEFAULT_SPEAKER
        if voice not in VALID_SPEAKERS:
            voice = DEFAULT_SPEAKER
        language = "french" if self._language.startswith("fr") else "english"

        loop = asyncio.get_running_loop()
        started = loop.time()
        first_audio = True

        try:
            chunks = await loop.run_in_executor(
                engine.executor,
                lambda: self._generate_audio_chunks(clean_text, voice, language),
            )

            for chunk in chunks:
                if first_audio:
                    first_audio = False
                    ttfa_ms = (loop.time() - started) * 1000
                    await self.stop_ttfb_metrics()
                    if self._latency_sink is not None:
                        res = self._latency_sink(
                            {
                                "type": "latency_metric",
                                "stage": "tts_ttfa",
                                "provider": "faster_qwen3_tts",
                                "model": self._model_id,
                                "milliseconds": round(ttfa_ms, 1),
                                "text_chars": len(clean_text),
                            }
                        )
                        if asyncio.iscoroutine(res):
                            await res

                resampled = soxr.resample(
                    chunk,
                    in_rate=QWEN3_SAMPLE_RATE,
                    out_rate=self._target_sample_rate,
                    quality="soxr_qq",
                )
                pcm16 = (np.clip(resampled, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
                yield TTSAudioRawFrame(
                    audio=pcm16,
                    sample_rate=self._target_sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )

        except Exception as exc:
            logger.exception("FasterQwen3TTS generation error text=%r", clean_text)
            yield ErrorFrame(error=f"FasterQwen3TTS error: {exc}")
        finally:
            await self.stop_ttfb_metrics()
