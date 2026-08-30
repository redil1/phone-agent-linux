"""Local Kokoro-82M TTS synthesized natively through PyTorch and CUDA.

The implementation runs Kokoro-82M on CUDA (with CPU fallback) via PyTorch,
achieving ~80x real-time generation on modern GPUs like the NVIDIA RTX A6000.
Audio generated at 24 kHz is converted via soxr to 16 kHz mono signed 16-bit PCM
for the PhoneAgent telephony bridge.
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
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts

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

logger = logging.getLogger("KokoroTTS")

# Kokoro renders at 24 kHz; the phone link takes 16 kHz mono PCM16.
KOKORO_SAMPLE_RATE = 24_000

# Canonical model identifiers mapped to the official PyTorch HuggingFace repo.
MODEL_REPOS = {
    "hexgrad/Kokoro-82M": "hexgrad/Kokoro-82M",
    "kokoro": "hexgrad/Kokoro-82M",
    "kokoro-82m": "hexgrad/Kokoro-82M",
    "kokoro-v1.0": "hexgrad/Kokoro-82M",
    "kokoro-bf16": "hexgrad/Kokoro-82M",
    "kokoro-4bit": "hexgrad/Kokoro-82M",
}
DEFAULT_MODEL = "hexgrad/Kokoro-82M"
DEFAULT_VOICE = "af_heart"


VALID_KOKORO_VOICES = {
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "af_aoede", "af_kore", "af_nova",
    "am_adam", "am_michael", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_onyx", "am_puck",
    "bf_emma", "bf_alice", "bf_isabella", "bf_lily", "bm_george", "bm_daniel", "bm_fable", "bm_lewis",
    "ff_siwis",
}


def _lang_code(value: str) -> str:
    """Map a runtime locale to the single letter Kokoro's G2P expects."""

    normalized = str(value).strip().lower().replace("_", "-")
    if normalized.startswith("fr"):
        return "f"
    if normalized.startswith("en-gb"):
        return "b"
    return "a"


def _resolve_voice(voice: str, language: str = "en-US") -> str:
    """Resolve requested voice to a valid Kokoro voice, falling back intelligently."""
    normalized = str(voice or "").strip()
    if normalized in VALID_KOKORO_VOICES:
        return normalized
    lang = _lang_code(language)
    if lang == "f":
        return "ff_siwis"
    if lang == "b":
        return "bf_emma"
    return DEFAULT_VOICE


def _resolve_repo(model: str) -> str:
    normalized = str(model).strip()
    if not normalized:
        return DEFAULT_MODEL
    if normalized in MODEL_REPOS:
        return MODEL_REPOS[normalized]
    if normalized.startswith("hexgrad/") or normalized.startswith("mlx-community/"):
        return "hexgrad/Kokoro-82M"
    raise ValueError(f"unsupported Kokoro model: {model}")


@dataclass
class _KokoroEngine:
    """One loaded PyTorch model pipeline plus the dedicated inference thread."""

    backend: Any
    executor: ThreadPoolExecutor
    device: str


_ENGINE_CACHE: dict[str, _KokoroEngine] = {}
_ENGINE_CACHE_LOCK = threading.Lock()


def _get_default_device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _load_engine(model: str, device: str | None = None) -> _KokoroEngine:
    """Load the Kokoro model once and serialize inference onto one worker."""

    repo = _resolve_repo(model)
    target_device = device or _get_default_device()
    cache_key = f"{repo}:{target_device}"

    with _ENGINE_CACHE_LOCK:
        cached = _ENGINE_CACHE.get(cache_key)
        if cached is not None:
            return cached

        from kokoro import KPipeline

        logger.info("loading Kokoro-82M pipeline on device=%s repo=%s", target_device, repo)
        pipeline = KPipeline(lang_code="a", repo_id=repo, device=target_device)
        engine = _KokoroEngine(
            backend=pipeline,
            executor=ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"phoneagent-kokoro-{target_device}",
            ),
            device=target_device,
        )
        _ENGINE_CACHE[cache_key] = engine
        return engine


def _waveform_to_pcm16(waveform: Any, target_rate: int) -> bytes:
    """Convert one 24 kHz model segment to clean mono telephone PCM."""

    try:
        import torch

        if isinstance(waveform, torch.Tensor):
            samples = waveform.detach().cpu().numpy().astype(np.float32).reshape(-1)
        else:
            samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
    except Exception:
        samples = np.asarray(waveform, dtype=np.float32).reshape(-1)

    if samples.size < 2:
        return b""
    samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    if KOKORO_SAMPLE_RATE != target_rate:
        samples = soxr.resample(samples, KOKORO_SAMPLE_RATE, target_rate, quality="HQ")
    samples = np.clip(samples, -1.0, 1.0)
    return np.rint(samples * 32767.0).astype("<i2", copy=False).tobytes()


def _render(
    engine: _KokoroEngine, text: str, voice: str, speed: float, lang: str
) -> list[Any]:
    """Run one synthesis pass to completion on the model's worker."""

    if getattr(engine.backend, "lang_code", "") != lang:
        engine.backend.lang_code = lang

    generator = engine.backend(text=text, voice=voice, speed=speed)
    audio_segments: list[Any] = []
    for item in generator:
        # KPipeline yields (graphemes, phonemes, audio) or (phonemes, audio)
        chunk = item[2] if len(item) == 3 else item[-1]
        if chunk is not None:
            audio_segments.append(chunk)
    return audio_segments


def prewarm_kokoro(
    model: str = DEFAULT_MODEL,
    voice: str = DEFAULT_VOICE,
    language: str = "en-US",
    device: str | None = None,
) -> float:
    """Load weights and prewarm CUDA kernels before the first caller turn."""

    import time

    started = time.perf_counter()
    resolved_voice = _resolve_voice(voice, language)
    engine = _load_engine(model, device=device)
    engine.executor.submit(
        _render, engine, "Ready.", resolved_voice, 1.0, _lang_code(language)
    ).result()
    return (time.perf_counter() - started) * 1000


@dataclass
class KokoroSettings(TTSSettings):
    """Runtime settings for the local Kokoro model."""

    voice: str = DEFAULT_VOICE
    lang: str = "en-US"
    speed: float = 1.0
    device: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class PhoneAgentKokoroTTSService(TTSService):
    """Kokoro-82M rendered on CUDA/CPU and delivered as phone PCM."""

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
        device: str | None = None,
        **kwargs: Any,
    ) -> None:
        resolved_voice = _resolve_voice(voice, lang)
        settings = self.Settings(
            model=model,
            voice=resolved_voice,
            lang=lang,
            speed=speed,
            device=device,
        )
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            settings=settings,
            **kwargs,
        )
        self._target_sample_rate = sample_rate
        self._model = model
        self._device = device
        self._engine: _KokoroEngine | None = None

    def _ensure_loaded(self) -> _KokoroEngine:
        if self._engine is None:
            self._engine = _load_engine(self._model, device=self._device)
        return self._engine

    def can_generate_metrics(self) -> bool:
        return True

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        phrase = text.strip()
        if not phrase:
            return

        lang_str = getattr(self._settings, "lang", "en-US") or "en-US"
        voice = _resolve_voice(assert_given(self._settings.voice) or DEFAULT_VOICE, lang_str)
        speed = getattr(self._settings, "speed", 1.0) or 1.0
        lang = _lang_code(lang_str)

        try:
            engine = await asyncio.to_thread(self._ensure_loaded)
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

            def _producer() -> None:
                try:
                    if getattr(engine.backend, "lang_code", "") != lang:
                        engine.backend.lang_code = lang
                    generator = engine.backend(text=phrase, voice=voice, speed=speed)
                    for item in generator:
                        chunk = item[2] if len(item) == 3 else item[-1]
                        if chunk is not None:
                            pcm = _waveform_to_pcm16(chunk, self._target_sample_rate)
                            if pcm:
                                loop.call_soon_threadsafe(queue.put_nowait, ("chunk", pcm))
                    loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
                except Exception as err:
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", err))

            engine.executor.submit(_producer)

            first = True
            while True:
                kind, data = await queue.get()
                if kind == "chunk":
                    if first:
                        await self.stop_ttfb_metrics()
                        first = False
                    yield TTSAudioRawFrame(
                        audio=data,
                        sample_rate=self._target_sample_rate,
                        num_channels=1,
                        context_id=context_id,
                    )
                elif kind == "done":
                    break
                elif kind == "error":
                    raise data
        except Exception as exc:
            logger.exception("Kokoro TTS synthesis failed")
            yield ErrorFrame(error=f"Kokoro TTS error: {exc}")
        finally:
            await self.stop_ttfb_metrics()
