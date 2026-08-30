"""Local Supertonic 2/3 adapter for the 16 kHz cellular audio pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import soxr
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings

try:
    from pipecat.services.settings import assert_given  # type: ignore[attr-defined]
except ImportError:
    def assert_given(value: Any) -> Any:
        try:
            from pipecat.services.settings import NOT_GIVEN, is_given
            if not is_given(value) or value is NOT_GIVEN:
                return ""
        except Exception:
            pass
        return value if value is not None else ""
from pipecat.services.tts_service import TextAggregationMode, TTSService
from pipecat.transcriptions.language import Language, resolve_language
from pipecat.utils.tracing.service_decorators import traced_tts

logger = logging.getLogger("PhoneAgentSupertonicTTS")
_SPEAKABLE_RE = re.compile(r"[\w\d]", re.UNICODE)


class PCMRenderer(Protocol):
    """Minimal fallback boundary; Edge implements this without pipeline nesting."""

    async def synthesize_pcm(self, text: str) -> bytes: ...

    async def cleanup(self) -> None: ...


class SupertonicBackend(Protocol):
    sample_rate: int
    model_name: str

    def get_voice_style(self, voice_name: str) -> Any: ...

    def synthesize(
        self,
        text: str,
        voice_style: Any,
        total_steps: int,
        speed: float,
        max_chunk_length: int,
        silence_duration: float,
        lang: str,
        verbose: bool,
    ) -> tuple[np.ndarray, np.ndarray]: ...


@dataclass(slots=True)
class _SupertonicEngine:
    backend: SupertonicBackend
    executor: ThreadPoolExecutor
    styles: dict[str, Any] = field(default_factory=dict)

    def voice_style(self, voice: str) -> Any:
        style = self.styles.get(voice)
        if style is None:
            style = self.backend.get_voice_style(voice)
            self.styles[voice] = style
        return style


_ENGINE_CACHE: dict[tuple[str, int, int], _SupertonicEngine] = {}
_ENGINE_CACHE_LOCK = threading.Lock()


def _load_engine(
    model: str,
    intra_op_threads: int = 0,
    inter_op_threads: int = 0,
) -> _SupertonicEngine:
    """Load each pinned model once and serialize its CPU inference work."""

    key = (model, intra_op_threads, inter_op_threads)
    with _ENGINE_CACHE_LOCK:
        cached = _ENGINE_CACHE.get(key)
        if cached is not None:
            return cached

        from supertonic import TTS

        backend = TTS(
            model=model,
            auto_download=True,
            intra_op_num_threads=intra_op_threads or None,
            inter_op_num_threads=inter_op_threads or None,
        )
        engine = _SupertonicEngine(
            backend=backend,
            executor=ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"phoneagent-{model}",
            ),
        )
        _ENGINE_CACHE[key] = engine
        logger.info(
            "loaded local Supertonic model=%s sample_rate=%s",
            model,
            backend.sample_rate,
        )
        return engine


def _base_language(value: str | Language) -> str:
    normalized = str(value).strip().lower().replace("_", "-")
    return "fr" if normalized.startswith("fr") else "en"


def _waveform_to_pcm16(waveform: np.ndarray, source_rate: int, target_rate: int) -> bytes:
    """Convert one model waveform to clean mono telephone PCM in one resampling pass."""

    samples = np.asarray(waveform, dtype=np.float32).squeeze()
    if samples.ndim != 1 or samples.size < 2:
        return b""
    samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    if source_rate != target_rate:
        samples = soxr.resample(samples, source_rate, target_rate, quality="HQ")
    samples = np.clip(samples, -1.0, 1.0)
    return np.rint(samples * 32767.0).astype("<i2", copy=False).tobytes()


def _trim_pcm(pcm: bytes, sample_rate: int) -> bytes:
    """Remove model padding from short reflexes while preserving their natural tail."""

    if not pcm or len(pcm) % 2:
        return b""
    samples = np.frombuffer(pcm, dtype="<i2")
    active = np.flatnonzero(np.abs(samples.astype(np.int32)) > 80)
    if active.size == 0:
        return b""
    start = max(0, int(active[0]) - int(sample_rate * 0.04))
    end = min(samples.size, int(active[-1]) + int(sample_rate * 0.10) + 1)
    return samples[start:end].tobytes()


@dataclass
class SupertonicSettings(TTSSettings):
    """Runtime settings for the local ONNX model."""

    steps: int = 8
    speed: float = 1.05


class PhoneAgentSupertonicTTSService(TTSService):
    """Fast local Supertonic synthesis with speculative reuse and Edge fallback."""

    Settings = SupertonicSettings
    _settings: SupertonicSettings

    def __init__(
        self,
        *,
        model: str = "supertonic-3",
        voice: str = "M1",
        language: str = "en",
        steps: int = 8,
        speed: float = 1.05,
        sample_rate: int = 16_000,
        max_chunk_length: int = 300,
        silence_duration: float = 0.18,
        frame_ms: int = 20,
        intra_op_threads: int = 0,
        inter_op_threads: int = 0,
        fallback_renderer: PCMRenderer | None = None,
        reflex_cache_dir: Path | None = None,
        engine: _SupertonicEngine | None = None,
        **kwargs: Any,
    ) -> None:
        if model not in {"supertonic-2", "supertonic-3"}:
            raise ValueError("Supertonic model must be supertonic-2 or supertonic-3")
        if not re.fullmatch(r"[MF][1-5]", voice):
            raise ValueError("Supertonic voice must be M1-M5 or F1-F5")
        if not 1 <= steps <= 100:
            raise ValueError("Supertonic steps must be between 1 and 100")
        if not 0.7 <= speed <= 2.0:
            raise ValueError("Supertonic speed must be between 0.7 and 2.0")
        if frame_ms not in {10, 20, 30, 40}:
            raise ValueError("Supertonic frame_ms must be 10, 20, 30, or 40")

        settings = self.Settings(
            model=model,
            voice=voice,
            language=_base_language(language),
            steps=steps,
            speed=speed,
        )
        super().__init__(
            sample_rate=sample_rate,
            text_aggregation_mode=TextAggregationMode.SENTENCE,
            push_start_frame=True,
            push_stop_frames=True,
            settings=settings,
            **kwargs,
        )
        self._target_sample_rate = sample_rate
        self._frame_bytes = sample_rate * frame_ms // 1000 * 2
        self._max_chunk_length = max_chunk_length
        self._silence_duration = silence_duration
        self._fallback_renderer = fallback_renderer
        self._engine = engine or _load_engine(model, intra_op_threads, inter_op_threads)
        self._prefetch_cache: dict[str, bytes] = {}
        self._prefetch_tasks: dict[str, asyncio.Task[bytes]] = {}
        self._reflex_cache_dir = reflex_cache_dir or (
            Path.home() / ".cache" / "phone-agent" / "supertonic-reflexes"
        )
        self._reflex_cache: dict[str, bytes] = {}
        self._reflex_tasks: dict[str, asyncio.Task[bytes]] = {}

    def can_generate_metrics(self) -> bool:
        return True

    def language_to_service_language(self, language: Language) -> str:
        mapping = {Language.EN: "en", Language.FR: "fr"}
        return resolve_language(language, mapping, use_base_code=True)

    async def _primary_pcm(self, phrase: str) -> bytes:
        voice = str(assert_given(self._settings.voice))
        language = _base_language(assert_given(self._settings.language))
        steps = int(self._settings.steps)
        speed = float(self._settings.speed)

        def synthesize() -> bytes:
            style = self._engine.voice_style(voice)
            waveform, _duration = self._engine.backend.synthesize(
                phrase,
                voice_style=style,
                total_steps=steps,
                speed=speed,
                max_chunk_length=self._max_chunk_length,
                silence_duration=self._silence_duration,
                lang=language,
                verbose=False,
            )
            return _waveform_to_pcm16(
                waveform,
                int(self._engine.backend.sample_rate),
                self._target_sample_rate,
            )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._engine.executor, synthesize)

    async def _render_pcm(self, phrase: str) -> tuple[bytes, str]:
        try:
            pcm = await self._primary_pcm(phrase)
            if not pcm:
                raise RuntimeError("Supertonic completed without audio")
            return pcm, self._engine.backend.model_name
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._fallback_renderer is None:
                raise
            logger.warning("Supertonic synthesis failed; using Edge fallback: %s", exc)
            pcm = await self._fallback_renderer.synthesize_pcm(phrase)
            if not pcm:
                raise RuntimeError("Supertonic and Edge fallback both returned no audio") from exc
            return pcm, "edge_tts_fallback"

    async def prefetch_text(self, text: str) -> None:
        phrase = text.strip()
        if not phrase or not _SPEAKABLE_RE.search(phrase) or phrase in self._prefetch_cache:
            return
        task = self._prefetch_tasks.get(phrase)
        if task is None:
            task = asyncio.create_task(
                self._prefetch_phrase(phrase),
                name="phoneagent-speculative-supertonic",
            )
            self._prefetch_tasks[phrase] = task
        await task

    async def _prefetch_phrase(self, phrase: str) -> bytes:
        try:
            pcm, provider = await self._render_pcm(phrase)
            if pcm:
                self._prefetch_cache[phrase] = pcm
                logger.debug("prefetched TTS provider=%s chars=%d", provider, len(phrase))
            return pcm
        finally:
            self._prefetch_tasks.pop(phrase, None)

    def clear_prefetch(self) -> None:
        for task in self._prefetch_tasks.values():
            if not task.done():
                task.cancel()
        self._prefetch_tasks.clear()
        self._prefetch_cache.clear()

    def has_ready_speculative_audio(self) -> bool:
        return any(self._prefetch_cache.values())

    def get_reflex_pcm(self, phrase: str) -> bytes | None:
        cached = self._reflex_cache.get(phrase)
        if cached:
            return cached
        path = self._reflex_path(phrase)
        try:
            pcm = path.read_bytes()
        except OSError:
            return None
        maximum = self._target_sample_rate * 2 * 5
        if not pcm or len(pcm) % 2 or len(pcm) > maximum:
            return None
        self._reflex_cache[phrase] = pcm
        return pcm

    async def warm_reflexes(self, phrases: tuple[str, ...]) -> None:
        tasks: list[asyncio.Task[bytes]] = []
        for raw_phrase in phrases:
            phrase = raw_phrase.strip()
            if not phrase or self.get_reflex_pcm(phrase):
                continue
            task = self._reflex_tasks.get(phrase)
            if task is None:
                task = asyncio.create_task(
                    self._warm_reflex(phrase),
                    name="phoneagent-supertonic-reflex-warmup",
                )
                self._reflex_tasks[phrase] = task
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks)

    async def _warm_reflex(self, phrase: str) -> bytes:
        try:
            pcm, _provider = await self._render_pcm(phrase)
            pcm = _trim_pcm(pcm, self._target_sample_rate)
            if not pcm:
                return b""
            self._reflex_cache[phrase] = pcm
            path = self._reflex_path(phrase)
            await asyncio.to_thread(self._write_atomic, path, pcm)
            return pcm
        finally:
            self._reflex_tasks.pop(phrase, None)

    def _reflex_path(self, phrase: str) -> Path:
        key = "\0".join(
            (
                "supertonic-reflex-v1",
                self._engine.backend.model_name,
                str(assert_given(self._settings.voice)),
                _base_language(assert_given(self._settings.language)),
                str(self._settings.steps),
                str(self._settings.speed),
                str(self._target_sample_rate),
                phrase,
            )
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self._reflex_cache_dir / f"{digest}.pcm"

    @staticmethod
    def _write_atomic(path: Path, pcm: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(pcm)
        temporary.replace(path)

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        phrase = text.strip()
        if not phrase or not _SPEAKABLE_RE.search(phrase):
            return

        emitted = False
        try:
            await self.start_tts_usage_metrics(phrase)
            pcm = self._prefetch_cache.pop(phrase, b"")
            provider = "speculative_cache" if pcm else ""
            if not pcm:
                task = self._prefetch_tasks.get(phrase)
                if task is not None:
                    pcm = await asyncio.shield(task)
                    provider = "speculative_wait"
            if not pcm:
                pcm, provider = await self._render_pcm(phrase)

            for offset in range(0, len(pcm), self._frame_bytes):
                chunk = pcm[offset : offset + self._frame_bytes]
                if not chunk:
                    continue
                if not emitted:
                    await self.stop_ttfb_metrics()
                    emitted = True
                    logger.info(
                        "Supertonic audio ready model=%s source=%s chars=%d",
                        self._engine.backend.model_name,
                        provider,
                        len(phrase),
                    )
                yield TTSAudioRawFrame(
                    audio=chunk,
                    sample_rate=self._target_sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )
            if not emitted:
                yield ErrorFrame(error="Supertonic completed without playable audio")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Supertonic TTS synthesis failed")
            yield ErrorFrame(error=f"Supertonic TTS error: {exc}")
        finally:
            await self.stop_ttfb_metrics()

    async def cleanup(self) -> None:
        self.clear_prefetch()
        tasks = tuple(self._reflex_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reflex_tasks.clear()
        if self._fallback_renderer is not None:
            await self._fallback_renderer.cleanup()
        await super().cleanup()


def prewarm_supertonic(
    *,
    model: str,
    voice: str,
    language: str,
    steps: int,
    speed: float,
    sample_rate: int,
    intra_op_threads: int = 0,
    inter_op_threads: int = 0,
) -> None:
    """Load weights, voice vectors, and ONNX kernels before accepting a call."""

    engine = _load_engine(model, intra_op_threads, inter_op_threads)
    style = engine.voice_style(voice)
    waveform, _duration = engine.backend.synthesize(
        "Ready.",
        voice_style=style,
        total_steps=steps,
        speed=speed,
        max_chunk_length=300,
        silence_duration=0.18,
        lang=_base_language(language),
        verbose=False,
    )
    if not _waveform_to_pcm16(waveform, engine.backend.sample_rate, sample_rate):
        raise RuntimeError("Supertonic prewarm produced no audio")
