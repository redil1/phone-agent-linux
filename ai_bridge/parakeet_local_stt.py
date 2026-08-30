"""Local Parakeet TDT speech recognition for the cellular downlink.

The remote bridge streams every chunk to Google and only finalizes a turn once
the provider agrees, which put roughly 850 ms on the critical path of every
answer and made the endpoint timer depend on a network round trip. Parakeet
runs on this machine at a real-time factor near 0.02, so the cheapest correct
design is the opposite one: buffer the caller's turn, endpoint on local
acoustics alone, and transcribe once when the turn closes.

Streaming the recognizer chunk by chunk was measured and rejected. At its
default context it costs more wall-clock than the audio it consumes and falls
behind the caller; a fully buffered single pass is both faster and more
accurate because it keeps the model's original global attention.

English and French only, matching the configured call policy.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
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

logger = logging.getLogger("ParakeetLocalSTT")

DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
SAMPLE_WIDTH = 2
_INT16_FULL_SCALE = 32768.0
# Speech kept after the last energetic frame so a closing consonant that
# falls under the energy gate is still transcribed.
_TAIL_PADDING_MS = 120
# No single agent utterance runs longer than this. Past it, a missing
# bot-stopped-speaking frame is assumed rather than trusted.
_BOT_SPEAKING_MAX_SECS = 30.0

SpeculationCandidateHandler = Callable[[str], Awaitable[None] | None]
SpeculationCancelHandler = Callable[[str], Awaitable[None] | None]

# One process-wide model, and one thread that owns every MLX call.
#
# MLX streams are thread-local: evaluating an array on a thread that has no
# registered stream raises "There is no Stream(cpu, 1) in current thread". The
# default asyncio executor hands work to arbitrary pooled threads, so inference
# must be pinned to a single thread we own. That also serializes GPU work
# against the local TTS engine, which shares the same device.
_MODEL_CACHE: dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def _inference_executor() -> ThreadPoolExecutor:
    """Return the single thread that owns this process's MLX stream."""

    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="phoneagent-parakeet"
            )
        return _EXECUTOR


def load_model(model_id: str = DEFAULT_MODEL) -> Any:
    """Load and cache the recognizer, returning the same instance thereafter."""

    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(model_id)
        if cached is not None:
            return cached
        try:
            from parakeet_mlx import from_pretrained

            started = time.perf_counter()
            model = from_pretrained(model_id)
            _MODEL_CACHE[model_id] = model
            logger.info(
                "loaded local Parakeet model=%s elapsed_ms=%.1f",
                model_id,
                (time.perf_counter() - started) * 1000,
            )
            return model
        except (ImportError, ModuleNotFoundError):
            # Fallback on Linux / CUDA environments using faster-whisper
            from faster_whisper import WhisperModel
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
            comp_type = "float16" if torch.cuda.is_available() else "default"
            whisper_model = "large-v3-turbo"
            started = time.perf_counter()
            model = WhisperModel(whisper_model, device=device, compute_type=comp_type)
            _MODEL_CACHE[model_id] = model
            logger.info(
                "loaded local faster-whisper CUDA fallback model=%s elapsed_ms=%.1f",
                whisper_model,
                (time.perf_counter() - started) * 1000,
            )
            return model


def transcribe_pcm(pcm: bytes, model_id: str = DEFAULT_MODEL) -> str:
    """Transcribe one fully buffered utterance of 16 kHz mono PCM16.

    Must run on the inference thread; call ``transcribe_pcm_async`` from the
    event loop rather than invoking this directly.
    """

    if len(pcm) < SAMPLE_WIDTH * 2:
        return ""

    model = load_model(model_id)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE

    if hasattr(model, "transcribe_stream"):
        import mlx.core as mx

        # The whole turn is already buffered, so windowing the attention would only
        # lose accuracy. keep_original_attention preserves the model's global
        # attention for the single pass.
        with model.transcribe_stream(
            context_size=(256, 256), keep_original_attention=True
        ) as stream:
            stream.add_audio(mx.array(samples))
            return stream.result.text.strip()
    elif hasattr(model, "transcribe"):
        # Faster-Whisper on Linux / CUDA: Use Silero VAD and hallucination suppression
        segments, _ = model.transcribe(
            samples,
            beam_size=1,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=250, speech_pad_ms=100),
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        text = " ".join(
            s.text.strip() for s in segments if getattr(s, "no_speech_prob", 0.0) < 0.7
        ).strip()
        # Reject common Whisper phantom silence hallucinations on background noise
        if text.lower().rstrip(".!?,") in {
            "thank you",
            "thank you.",
            "thank you very much",
            "thanks for watching",
            "gracias",
            "muchas gracias",
            "subtitles by",
            "merci",
            "merci beaucoup",
            "you",
            "bye",
        }:
            return ""
        return text
    return ""


async def transcribe_pcm_async(pcm: bytes, model_id: str = DEFAULT_MODEL) -> str:
    """Transcribe off the event loop, on the thread that owns the MLX stream."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _inference_executor(), lambda: transcribe_pcm(pcm, model_id)
    )


def prewarm_parakeet(model_id: str = DEFAULT_MODEL) -> float:
    """Load weights and compile the graph before the first caller speaks.

    Runs on the inference thread so the compiled graph and the MLX stream are
    warmed on the exact thread that will serve live calls.
    """

    started = time.perf_counter()
    _inference_executor().submit(
        transcribe_pcm, b"\x00" * (16_000 * SAMPLE_WIDTH), model_id
    ).result()
    return (time.perf_counter() - started) * 1000


def _calc_dbfs(audio: bytes) -> float:
    """RMS level of 16-bit mono PCM, used only for endpointing."""

    if len(audio) < SAMPLE_WIDTH:
        return -120.0
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return -120.0
    mean_square = float(np.mean(samples * samples))
    if mean_square <= 0.0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(mean_square) / _INT16_FULL_SCALE)


class ParakeetLocalSTTService(STTService):
    """Buffer a caller turn, endpoint locally, and transcribe it in one pass."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        language: str = "en-US",
        model: str = DEFAULT_MODEL,
        endpoint_ms: int = 1000,
        incomplete_endpoint_ms: int = 1400,
        prefetch_silence_ms: int = 180,
        energy_threshold_dbfs: float = -42.0,
        min_utterance_ms: int = 240,
        max_utterance_secs: int = 30,
        echo_guard_db: float = 0.0,
        min_chars_per_second: float = 5.0,
        hallucination_audio_ms: int = 3000,
        speculative_pipeline_enabled: bool = False,
        **kwargs: Any,
    ) -> None:
        # audio_passthrough=False keeps caller audio out of the output
        # transport, so nothing the caller says can echo back down the uplink.
        super().__init__(audio_passthrough=False, sample_rate=sample_rate, **kwargs)
        if not language.lower().startswith(("en", "fr")):
            raise ValueError("ParakeetLocalSTTService supports English and French only")
        self._model_id = model
        self._language = language
        self._endpoint_sec = endpoint_ms / 1000.0
        self._incomplete_endpoint_sec = max(incomplete_endpoint_ms, endpoint_ms) / 1000.0
        self._prefetch_silence_sec = prefetch_silence_ms / 1000.0
        self._energy_threshold_dbfs = energy_threshold_dbfs
        self._min_utterance_bytes = 16_000 * SAMPLE_WIDTH * min_utterance_ms // 1000
        self._tail_padding_bytes = 16_000 * SAMPLE_WIDTH * _TAIL_PADDING_MS // 1000
        self._max_utterance_bytes = 16_000 * SAMPLE_WIDTH * max_utterance_secs
        # Far-end echo returns attenuated, so while the agent is speaking only
        # audio clearly louder than the echo floor counts as the caller. Raising
        # the bar rather than muting keeps genuine barge-in working.
        self._echo_guard_db = echo_guard_db
        self._min_chars_per_second = min_chars_per_second
        self._hallucination_audio_bytes = (
            16_000 * SAMPLE_WIDTH * hallucination_audio_ms // 1000
        )
        self._bot_speaking = False
        self._bot_speaking_since = 0.0
        self._speculative_pipeline_enabled = speculative_pipeline_enabled

        self._buffer = bytearray()
        self._buffer_lock = asyncio.Lock()
        self._speaking = False
        self._speech_seen = False
        self._speech_bytes = 0
        self._last_speech_at = 0.0
        self._watchdog_task: asyncio.Task | None = None
        self._closing = False

        self._prefetch_text = ""
        self._prefetch_bytes = -1
        self._prefetch_task: asyncio.Task | None = None
        self._speculation_candidate_handler: SpeculationCandidateHandler | None = None
        self._speculation_cancel_handler: SpeculationCancelHandler | None = None

    # ------------------------------------------------------------------ setup

    def set_speculation_handlers(
        self,
        candidate_handler: SpeculationCandidateHandler | None,
        cancel_handler: SpeculationCancelHandler | None,
    ) -> None:
        """Attach the optional speculative turn hooks used by the pipeline."""

        self._speculation_candidate_handler = candidate_handler
        self._speculation_cancel_handler = cancel_handler

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._closing = False
        # Loading here would block the pipeline; prewarm normally did it first.
        await asyncio.get_running_loop().run_in_executor(
            _inference_executor(), load_model, self._model_id
        )
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(
                self._endpoint_watchdog(), name="parakeet_endpoint_watchdog"
            )
        logger.info(
            "local Parakeet STT ready model=%s language=%s endpoint_ms=%d",
            self._model_id,
            self._language,
            int(self._endpoint_sec * 1000),
        )

    async def stop(self, frame: EndFrame) -> None:
        await self._shutdown()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._shutdown()
        await super().cancel(frame)

    async def cleanup(self) -> None:
        await self._shutdown()
        await super().cleanup()

    async def _shutdown(self) -> None:
        self._closing = True
        tasks = [self._watchdog_task, self._prefetch_task]
        self._watchdog_task = None
        self._prefetch_task = None
        pending = [t for t in tasks if t is not None and not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------ audio

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        # The output transport reports bot speech upstream, which is how this
        # service learns not to transcribe the agent's own voice coming back
        # down the line as if the caller had said it.
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._bot_speaking_since = time.monotonic()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._bot_speaking_since = 0.0
        await super().process_frame(frame, direction)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Accumulate caller audio and track speech energy for endpointing."""

        if not audio or self._closing:
            yield None
            return

        now = time.monotonic()
        threshold = self._energy_threshold_dbfs
        if self._bot_speaking and now - self._bot_speaking_since > _BOT_SPEAKING_MAX_SECS:
            # A stop frame was missed. Without this the gate stays raised for
            # the rest of the call and the caller is simply never heard.
            logger.warning("Bot-speaking state timed out; clearing the echo gate")
            self._bot_speaking = False
            self._bot_speaking_since = 0.0
        if self._echo_guard_db and self._bot_speaking:
            # Raise the bar rather than muting: a real barge-in is much louder
            # than echo, so interruption still works.
            threshold += self._echo_guard_db
        speech = _calc_dbfs(audio) >= threshold
        async with self._buffer_lock:
            if speech:
                self._last_speech_at = now
                if not self._speech_seen:
                    self._speech_seen = True
            # Buffer unconditionally once speech has started so the leading
            # consonant of a word is never clipped off the front of a turn.
            if self._speech_seen:
                self._buffer.extend(audio)
                if speech:
                    # Remember where speech last ended. Everything after this is
                    # the endpoint pause, and feeding that trailing silence to
                    # the recognizer only makes the pass slower and defeats
                    # reuse of the speculative result, because the buffer would
                    # keep growing while the caller says nothing.
                    self._speech_bytes = len(self._buffer)
                if len(self._buffer) > self._max_utterance_bytes:
                    # Bound memory on a caller who never pauses. Drop the oldest
                    # audio rather than the most recent, which carries intent.
                    del self._buffer[: len(self._buffer) - self._max_utterance_bytes]

        if speech and not self._speaking:
            self._speaking = True
            await self.push_frame(UserStartedSpeakingFrame())
        yield None

    async def _endpoint_watchdog(self) -> None:
        """Close the turn on local silence alone, with no provider dependency."""

        try:
            while not self._closing:
                await asyncio.sleep(0.02)
                async with self._buffer_lock:
                    speech_bytes = self._speech_bytes
                    speech_seen = self._speech_seen
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
                    # Never await inference here. Blocking this loop would delay
                    # the endpoint decision by a whole model pass, which is the
                    # exact latency this service exists to remove.
                    self._prefetch_task = asyncio.create_task(
                        self._run_prefetch(speech_bytes)
                    )

                # A trailing conjunction means the caller is mid-thought; give
                # them longer before taking the turn away from them.
                required = self._endpoint_sec
                if self._prefetch_text and looks_semantically_incomplete(self._prefetch_text):
                    required = self._incomplete_endpoint_sec
                if silence >= required:
                    await self._commit_turn()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("local Parakeet endpoint watchdog failed")
            await self.push_frame(ErrorFrame(error=f"Parakeet endpointing error: {exc}"))

    async def _speech_snapshot(self) -> tuple[bytes, int]:
        """Copy the buffered turn with the endpoint pause trimmed off the end.

        A short tail is kept so the closing consonant is never clipped. The
        returned length is the trim point, which is what identifies this audio
        for speculative reuse.
        """

        async with self._buffer_lock:
            speech_bytes = self._speech_bytes
            keep = min(len(self._buffer), speech_bytes + self._tail_padding_bytes)
            return bytes(self._buffer[:keep]), speech_bytes

    async def _run_prefetch(self, buffered: int) -> None:
        """Transcribe the pause-so-far to seed speculation and endpoint choice."""

        snapshot, _ = await self._speech_snapshot()
        try:
            text = await transcribe_pcm_async(snapshot, self._model_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("speculative local transcription failed", exc_info=True)
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
        """Emit exactly one authoritative caller turn and reset for the next."""

        snapshot, speech_bytes = await self._speech_snapshot()
        async with self._buffer_lock:
            self._buffer.clear()
            self._speech_seen = False
            self._speech_bytes = 0
        prefetch_text, prefetch_bytes = self._prefetch_text, self._prefetch_bytes
        self._prefetch_text, self._prefetch_bytes = "", -1

        # The speculative pass already transcribed exactly this speech, so
        # re-running the model would spend another pass to produce the identical
        # string. Trimming the pause is what makes this match reliably.
        if prefetch_text and prefetch_bytes == speech_bytes:
            text = prefetch_text
        else:
            try:
                text = await transcribe_pcm_async(snapshot, self._model_id)
            except Exception as exc:
                logger.exception("local transcription failed")
                await self.push_frame(ErrorFrame(error=f"Parakeet transcription failed: {exc}"))
                await self._end_speaking()
                return

        # A whitespace-only hypothesis is not a caller turn. Normalize here
        # rather than trusting the recognizer, so a model or version that
        # returns padding cannot inject an empty turn into the LLM context.
        text = (text or "").strip()
        if text and self._looks_hallucinated(text, len(snapshot)):
            audio_ms = len(snapshot) // (16 * SAMPLE_WIDTH)
            logger.warning(
                "Discarded a hallucinated transcript chars=%d audio_ms=%d text=%r",
                len(text),
                audio_ms,
                text[:60],
            )
            await self._invoke(self._speculation_cancel_handler, "hallucinated_transcript")
            await self._end_speaking()
            return
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
            "Committed stable caller turn source=local_parakeet chars=%d audio_ms=%d",
            len(text),
            len(snapshot) // (16 * SAMPLE_WIDTH),
        )

    def _looks_hallucinated(self, text: str, audio_bytes: int) -> bool:
        """Reject a transcript far too short for the audio that produced it.

        Recognizers invent plausible sentences from line noise, breathing or
        returning echo. Real speech runs about twelve characters a second; a
        long window yielding a handful of characters was not speech. Short
        windows are exempt, because a genuine "Yes." is legitimately brief.
        """

        if audio_bytes < self._hallucination_audio_bytes:
            return False
        seconds = audio_bytes / (16_000 * SAMPLE_WIDTH)
        return (len(text) / seconds) < self._min_chars_per_second

    async def _end_speaking(self) -> None:
        if self._speaking:
            self._speaking = False
            await self.push_frame(UserStoppedSpeakingFrame())

    @staticmethod
    async def _invoke(handler: Callable[[str], Any] | None, value: str) -> None:
        if handler is None:
            return
        result = handler(value)
        if inspect.isawaitable(result):
            await result
