"""Local Kokoro-82M TTS on Apple Silicon, synthesized through MLX/Metal.

The previous implementation ran Kokoro through ONNX Runtime's CPU execution
provider, which measured 4.1x realtime on this hardware -- more than two seconds
of compute for a ten-second reply, which a telephone caller hears as dead air.
The same weights driven through MLX measure 26.9x on the same text and machine,
so the model is loaded with ``mlx_audio`` and the ONNX path is gone.

Do not move this to PyTorch with the ``mps`` device: ``aten::angle`` is missing
there and the vocoder falls back to CPU per operation, landing slower than plain
PyTorch CPU.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import soxr
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings, assert_given
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts

logger = logging.getLogger("KokoroTTS")

# Kokoro renders at 24 kHz; the phone link takes 16 kHz mono PCM16.
KOKORO_SAMPLE_RATE = 24_000

# bf16 is the default because it measured faster than the 4-bit quantization on
# this machine (26.9x against 17.8x), which inverts the ordering published for
# the M1 Max. The quantization stays selectable for memory-constrained runs.
MODEL_REPOS = {
    "kokoro-bf16": "mlx-community/Kokoro-82M-bf16",
    "kokoro-4bit": "mlx-community/Kokoro-82M-4bit",
}
DEFAULT_MODEL = "kokoro-bf16"
DEFAULT_VOICE = "af_heart"


def _lang_code(value: str) -> str:
    """Map a runtime locale to the single letter Kokoro's G2P expects."""

    normalized = str(value).strip().lower().replace("_", "-")
    if normalized.startswith("fr"):
        return "f"
    if normalized.startswith("en-gb"):
        return "b"
    return "a"


def _resolve_repo(model: str) -> str:
    if model in MODEL_REPOS:
        return MODEL_REPOS[model]
    if model.startswith("mlx-community/"):
        return model
    raise ValueError(f"unsupported Kokoro model: {model!r}")


@dataclass
class _KokoroEngine:
    """One loaded model plus the single thread its Metal work runs on."""

    backend: Any
    executor: ThreadPoolExecutor


_ENGINE_CACHE: dict[str, _KokoroEngine] = {}
_ENGINE_CACHE_LOCK = threading.Lock()


def _load_engine(model: str) -> _KokoroEngine:
    """Load each repo once and serialize its inference onto one worker.

    Metal command buffers from concurrent calls interleave badly and the model
    is small enough that one stream already saturates it, so a single worker is
    both simpler and faster than contending for the GPU.
    """

    repo = _resolve_repo(model)
    with _ENGINE_CACHE_LOCK:
        cached = _ENGINE_CACHE.get(repo)
        if cached is not None:
            return cached

        from mlx_audio.tts.utils import load_model

        backend = load_model(repo)
        engine = _KokoroEngine(
            backend=backend,
            executor=ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"phoneagent-{model}",
            ),
        )
        _ENGINE_CACHE[repo] = engine
        logger.info("loaded local Kokoro model=%s via MLX", repo)
        return engine


def _waveform_to_pcm16(waveform: Any, target_rate: int) -> bytes:
    """Convert one 24 kHz model segment to clean mono telephone PCM.

    Resampling goes through soxr rather than a linear interpolation, which the
    previous implementation used and which aliases audibly on speech.
    """

    samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if samples.size < 2:
        return b""
    samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    if KOKORO_SAMPLE_RATE != target_rate:
        samples = soxr.resample(samples, KOKORO_SAMPLE_RATE, target_rate, quality="HQ")
    samples = np.clip(samples, -1.0, 1.0)
    return np.rint(samples * 32767.0).astype("<i2", copy=False).tobytes()


def _render(engine: _KokoroEngine, text: str, voice: str, speed: float, lang: str) -> list[Any]:
    """Run one synthesis pass to completion on the model's own worker."""

    import mlx.core as mx

    segments = list(
        engine.backend.generate(text=text, voice=voice, speed=speed, lang_code=lang)
    )
    audio = [np.asarray(segment.audio, dtype=np.float32).reshape(-1) for segment in segments]
    # Metal work is queued lazily. Without this the timing above would stop
    # before the GPU had finished and the first frames would arrive late.
    mx.eval(mx.array(0))
    return audio


def prewarm_kokoro(
    model: str = DEFAULT_MODEL,
    voice: str = DEFAULT_VOICE,
    language: str = "en-US",
) -> float:
    """Load weights and compile Metal kernels before the first caller turn."""

    import time

    started = time.perf_counter()
    engine = _load_engine(model)
    engine.executor.submit(_render, engine, "Ready.", voice, 1.0, _lang_code(language)).result()
    return (time.perf_counter() - started) * 1000


@dataclass
class KokoroSettings(TTSSettings):
    """Runtime settings for the local MLX model."""

    voice: str = DEFAULT_VOICE
    lang: str = "en-US"
    speed: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)


class PhoneAgentKokoroTTSService(TTSService):
    """Kokoro-82M rendered locally on Metal and delivered as phone PCM."""

    Settings = KokoroSettings
    _settings: KokoroSettings

    def __init__(
        self,
        *,
        voice: str = DEFAULT_VOICE,
        lang: str = "en-US",
        speed: float = 1.0,
        sample_rate: int = 16_000,
        model: str = DEFAULT_MODEL,
        **kwargs: Any,
    ) -> None:
        settings = self.Settings(model=model, voice=voice, lang=lang, speed=speed)
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            settings=settings,
            **kwargs,
        )
        self._target_sample_rate = sample_rate
        self._model = model
        self._engine: _KokoroEngine | None = None

    def _ensure_loaded(self) -> _KokoroEngine:
        if self._engine is None:
            self._engine = _load_engine(self._model)
        return self._engine

    def can_generate_metrics(self) -> bool:
        return True

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        phrase = text.strip()
        if not phrase:
            return

        voice = assert_given(self._settings.voice) or DEFAULT_VOICE
        speed = getattr(self._settings, "speed", 1.0) or 1.0
        lang = _lang_code(getattr(self._settings, "lang", "en-US") or "en-US")

        try:
            engine = await asyncio.to_thread(self._ensure_loaded)
            segments = await asyncio.wrap_future(
                engine.executor.submit(_render, engine, phrase, voice, speed, lang)
            )
            await self.stop_ttfb_metrics()
            for segment in segments:
                pcm = _waveform_to_pcm16(segment, self._target_sample_rate)
                if pcm:
                    yield TTSAudioRawFrame(
                        audio=pcm,
                        sample_rate=self._target_sample_rate,
                        num_channels=1,
                        context_id=context_id,
                    )
        except Exception as exc:
            logger.exception("Kokoro TTS synthesis failed")
            yield ErrorFrame(error=f"Kokoro TTS error: {exc}")
        finally:
            await self.stop_ttfb_metrics()
