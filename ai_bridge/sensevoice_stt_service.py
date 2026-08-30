"""SenseVoice-Small Non-Autoregressive Speech Recognition for Real-time Telephony.

SenseVoice-Small uses a single-pass Non-Autoregressive (NAR) architecture that
transcribes audio in ~20-50ms (over 10x faster than Whisper) and eliminates
autoregressive hallucinations on background noise and silence.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import STTService

from .turn_continuity import looks_semantically_incomplete

logger = logging.getLogger("SenseVoiceSTT")

DEFAULT_MODEL = "iic/SenseVoiceSmall"
SAMPLE_WIDTH = 2
_INT16_FULL_SCALE = 32768.0
_TAIL_PADDING_MS = 120
_BOT_SPEAKING_MAX_SECS = 30.0

SpeculationCandidateHandler = Callable[[str], Awaitable[None] | None]
SpeculationCancelHandler = Callable[[str], Awaitable[None] | None]

_MODEL_CACHE: dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def _inference_executor() -> ThreadPoolExecutor:
    """Return the single worker thread that owns CUDA inference for SenseVoice."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="phoneagent-sensevoice"
            )
        return _EXECUTOR


def load_sensevoice_model(model_id: str = DEFAULT_MODEL, device: str | None = None) -> Any:
    """Load and cache SenseVoice-Small in GPU memory."""
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(model_id)
        if cached is not None:
            return cached

        import torch
        from funasr import AutoModel

        target_device = device or ("cuda:0" if torch.cuda.is_available() else "cpu")
        started = time.perf_counter()
        logger.info("Loading SenseVoice-Small on device=%s from hub=ms", target_device)

        model = AutoModel(
            model=model_id,
            device=target_device,
            disable_update=True,
            hub="ms",
        )
        _MODEL_CACHE[model_id] = model
        logger.info(
            "Loaded SenseVoice-Small model=%s on %s in %.1fms",
            model_id,
            target_device,
            (time.perf_counter() - started) * 1000,
        )
        return model


def _calc_dbfs(audio: bytes) -> float:
    """RMS level of 16-bit mono PCM in dBFS."""
    if len(audio) < SAMPLE_WIDTH:
        return -120.0
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return -120.0
    mean_square = float(np.mean(samples * samples))
    if mean_square <= 0.0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(mean_square) / _INT16_FULL_SCALE)


_SPECIAL_TAG_RE = re.compile(r"<\|.*?\|>")
_WORD_CHAR_RE = re.compile(r"[\w\d]", re.UNICODE)


def transcribe_pcm(
    pcm: bytes,
    model_id: str = DEFAULT_MODEL,
    language: str = "auto",
    device: str | None = None,
) -> str:
    """Transcribe buffered 16 kHz mono PCM16 audio in a single forward pass."""
    if len(pcm) < SAMPLE_WIDTH * 160:  # < 10ms
        return ""

    # Noise gate: ignore audio with negligible acoustic energy
    dbfs = _calc_dbfs(pcm)
    if dbfs < -50.0:
        return ""

    model = load_sensevoice_model(model_id, device=device)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE

    # Normalize language string for SenseVoice (e.g. 'en-US' -> 'en', 'fr-FR' -> 'fr')
    lang = (language or "auto").split("-")[0].lower()
    if lang not in {"en", "fr", "es", "zh", "ja", "ko", "yue", "auto"}:
        lang = "auto"

    try:
        res = model.generate(
            input=samples,
            cache={},
            language=lang,
            use_itn=True,
            batch_size_s=60,
        )
        if not res:
            return ""
        raw_text = res[0].get("text", "")
        # Remove rich event and language tags (<|en|>, <|Speech|>, <|NEUTRAL|>, etc.)
        clean_text = _SPECIAL_TAG_RE.sub("", raw_text).strip()
        # If output contains only punctuation without any words, treat as silence
        if not _WORD_CHAR_RE.search(clean_text):
            return ""
        return clean_text
    except Exception as exc:
        logger.warning("SenseVoice inference error: %s", exc)
        return ""


async def transcribe_pcm_async(
    pcm: bytes,
    model_id: str = DEFAULT_MODEL,
    language: str = "auto",
) -> str:
    """Asynchronously run transcription on the dedicated CUDA worker thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _inference_executor(), lambda: transcribe_pcm(pcm, model_id, language)
    )


def prewarm_sensevoice(model_id: str = DEFAULT_MODEL, device: str | None = None) -> float:
    """Pre-warm SenseVoice-Small weights and CUDA kernels on startup."""
    started = time.perf_counter()
    load_sensevoice_model(model_id, device=device)
    # Generate 0.5s of synthetic test audio to trigger first-pass CUDA kernel compilation
    synthetic_pcm = (np.sin(np.linspace(0, 440 * 2 * np.pi * 0.5, 8000)) * 12000).astype(np.int16).tobytes()
    _inference_executor().submit(
        transcribe_pcm, synthetic_pcm, model_id, "auto", device
    ).result()
    return (time.perf_counter() - started) * 1000


class SenseVoiceSTTService(STTService):
    """Buffer caller audio turns and transcribe via resident SenseVoice-Small on CUDA."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        language: str = "en-US",
        model: str = DEFAULT_MODEL,
        endpoint_ms: int = 800,
        incomplete_endpoint_ms: int = 1200,
        prefetch_silence_ms: int = 160,
        energy_threshold_dbfs: float = -44.0,
        min_utterance_ms: int = 120,
        max_utterance_secs: int = 30,
        echo_guard_db: float = 0.0,
        min_chars_per_second: float = 4.0,
        hallucination_audio_ms: int = 2000,
        speculative_pipeline_enabled: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(audio_passthrough=False, sample_rate=sample_rate, **kwargs)
        self._model_id = model
        self._language = language
        self._endpoint_sec = endpoint_ms / 1000.0
        self._incomplete_endpoint_sec = incomplete_endpoint_ms / 1000.0
        self._prefetch_silence_sec = prefetch_silence_ms / 1000.0
        self._energy_threshold_dbfs = energy_threshold_dbfs
        self._min_utterance_bytes = int(sample_rate * SAMPLE_WIDTH * (min_utterance_ms / 1000.0))
        self._max_utterance_bytes = int(sample_rate * SAMPLE_WIDTH * max_utterance_secs)
        self._tail_padding_bytes = int(sample_rate * SAMPLE_WIDTH * (_TAIL_PADDING_MS / 1000.0))
        self._echo_guard_db = echo_guard_db
        self._min_chars_per_second = min_chars_per_second
        self._hallucination_audio_bytes = int(sample_rate * SAMPLE_WIDTH * (hallucination_audio_ms / 1000.0))
        self._speculative_pipeline_enabled = speculative_pipeline_enabled

        self._buffer = bytearray()
        self._buffer_lock = asyncio.Lock()
        self._speech_seen = False
        self._speech_bytes = 0
        self._last_speech_at = 0.0
        self._last_frame_at = 0.0
        self._bot_speaking = False
        self._bot_speaking_at = 0.0
        self._speaking = False
        self._watchdog_task: asyncio.Task[None] | None = None
        self._prefetch_task: asyncio.Task[None] | None = None
        self._prefetch_text = ""
        self._prefetch_bytes = -1

        self._speculation_candidate_handler: SpeculationCandidateHandler | None = None
        self._speculation_cancel_handler: SpeculationCancelHandler | None = None

    def set_speculation_candidate_handler(
        self, handler: SpeculationCandidateHandler | None
    ) -> None:
        self._speculation_candidate_handler = handler

    def set_speculation_cancel_handler(
        self, handler: SpeculationCancelHandler | None
    ) -> None:
        self._speculation_cancel_handler = handler

    async def _invoke(self, handler: Any, *args: Any) -> None:
        if handler is None:
            return
        res = handler(*args)
        if inspect.isawaitable(res):
            await res

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop(self, frame: EndFrame) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
            self._prefetch_task = None
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
            self._prefetch_task = None
        await super().cancel(frame)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._bot_speaking_at = time.monotonic()
            async with self._buffer_lock:
                self._buffer.clear()
                self._speech_seen = False
                self._speech_bytes = 0
            self._prefetch_text, self._prefetch_bytes = "", -1
            await self._end_speaking()
            return

        if isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            return

    async def _end_speaking(self) -> None:
        if self._speaking:
            self._speaking = False
            await self.push_frame(UserStoppedSpeakingFrame())

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Process incoming audio frames and buffer for endpointing."""
        if not audio:
            yield None
            return

        now = time.monotonic()
        if self._bot_speaking and (now - self._bot_speaking_at > _BOT_SPEAKING_MAX_SECS):
            self._bot_speaking = False

        effective_threshold = self._energy_threshold_dbfs + (
            self._echo_guard_db if self._bot_speaking else 0.0
        )
        is_speech = _calc_dbfs(audio) >= effective_threshold

        async with self._buffer_lock:
            if not self._speech_seen:
                if not is_speech:
                    yield None
                    return
                self._speech_seen = True
                self._buffer.clear()
                self._speech_bytes = 0
                if not self._speaking:
                    self._speaking = True
                    await self.push_frame(UserStartedSpeakingFrame())

            self._buffer.extend(audio)
            if is_speech:
                self._speech_bytes = len(self._buffer)
                self._last_speech_at = now
            self._last_frame_at = now

            if len(self._buffer) >= self._max_utterance_bytes:
                await self._commit_turn()

        yield None

    async def _watchdog_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.02)
                async with self._buffer_lock:
                    speech_seen = self._speech_seen
                    speech_bytes = self._speech_bytes
                    last_speech_at = self._last_speech_at

                if not speech_seen or speech_bytes < self._min_utterance_bytes:
                    continue

                silence = time.monotonic() - last_speech_at
                if (
                    self._speculative_pipeline_enabled
                    and silence >= self._prefetch_silence_sec
                    and speech_bytes != self._prefetch_bytes
                    and (self._prefetch_task is None or self._prefetch_task.done())
                ):
                    self._prefetch_task = asyncio.create_task(
                        self._run_prefetch(speech_bytes)
                    )

                required = self._endpoint_sec
                if self._prefetch_text and looks_semantically_incomplete(self._prefetch_text):
                    required = self._incomplete_endpoint_sec
                if silence >= required:
                    await self._commit_turn()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("SenseVoice endpoint watchdog failed: %s", exc)

    async def _speech_snapshot(self) -> tuple[bytes, int]:
        async with self._buffer_lock:
            speech_bytes = self._speech_bytes
            keep = min(len(self._buffer), speech_bytes + self._tail_padding_bytes)
            return bytes(self._buffer[:keep]), speech_bytes

    async def _run_prefetch(self, buffered: int) -> None:
        snapshot, _ = await self._speech_snapshot()
        try:
            text = await transcribe_pcm_async(snapshot, self._model_id, self._language)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        self._prefetch_bytes = buffered
        text = (text or "").strip()
        if not text or text == self._prefetch_text:
            return
        self._prefetch_text = text
        await self.push_frame(
            InterimTranscriptionFrame(text=text, user_id="caller", timestamp=None)
        )
        await self._invoke(self._speculation_candidate_handler, text)

    async def _commit_turn(self) -> None:
        snapshot, speech_bytes = await self._speech_snapshot()
        async with self._buffer_lock:
            self._buffer.clear()
            self._speech_seen = False
            self._speech_bytes = 0
        prefetch_text, prefetch_bytes = self._prefetch_text, self._prefetch_bytes
        self._prefetch_text, self._prefetch_bytes = "", -1

        if prefetch_text and prefetch_bytes == speech_bytes:
            text = prefetch_text
        else:
            try:
                text = await transcribe_pcm_async(snapshot, self._model_id, self._language)
            except Exception as exc:
                logger.exception("SenseVoice transcription failed: %s", exc)
                await self._end_speaking()
                return

        text = (text or "").strip()
        if not text:
            await self._invoke(self._speculation_cancel_handler, "empty_transcript")
            await self._end_speaking()
            return

        if not self._speaking:
            self._speaking = True
            await self.push_frame(UserStartedSpeakingFrame())
        await self.push_frame(TranscriptionFrame(text=text, user_id="caller", timestamp=None))
        await self._end_speaking()
        logger.info(
            "Committed stable caller turn source=sensevoice chars=%d audio_ms=%d: %r",
            len(text),
            len(snapshot) // (16 * SAMPLE_WIDTH),
            text,
        )
