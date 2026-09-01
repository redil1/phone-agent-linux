"""Unit tests for the PyTorch/CUDA-backed Kokoro service and its telephony conversion."""

from __future__ import annotations

import numpy as np
import pytest
from pipecat.frames.frames import TTSAudioRawFrame

from phone_agent_gateway.ai_bridge.kokoro_tts_service import (
    PhoneAgentKokoroTTSService,
    _KokoroEngine,
    _lang_code,
    _resolve_repo,
    _waveform_to_pcm16,
)


def test_waveform_is_resampled_to_phone_pcm() -> None:
    # 0.1 s of 24 kHz tone -> 0.1 s of 16 kHz signed 16-bit mono.
    t = np.linspace(0, 0.1, 2400, dtype=np.float32)
    samples = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    pcm = np.frombuffer(_waveform_to_pcm16(samples, 16_000), dtype="<i2")

    assert 1500 <= len(pcm) <= 1700
    assert np.max(np.abs(pcm)) > 5000


def test_non_finite_samples_cannot_reach_the_phone() -> None:
    samples = np.array([0.0, np.nan, np.inf, -np.inf, 0.5] * 200, dtype=np.float32)

    pcm = np.frombuffer(_waveform_to_pcm16(samples, 16_000), dtype="<i2")

    assert len(pcm) > 0
    assert np.all(np.isfinite(pcm.astype(np.float32)))


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("fr-FR", "f"),
        ("fr", "f"),
        ("en-US", "a"),
        ("en", "a"),
        ("en-GB", "b"),
        ("en_US", "a"),
    ],
)
def test_locale_maps_to_a_kokoro_lang_code(locale: str, expected: str) -> None:
    # Kokoro's G2P takes a single letter, not an espeak tag.
    assert _lang_code(locale) == expected


def test_known_repos_and_aliases_are_accepted() -> None:
    assert _resolve_repo("hexgrad/Kokoro-82M") == "hexgrad/Kokoro-82M"
    assert _resolve_repo("kokoro-82m") == "hexgrad/Kokoro-82M"
    assert _resolve_repo("kokoro-bf16") == "hexgrad/Kokoro-82M"
    assert _resolve_repo("kokoro-4bit") == "hexgrad/Kokoro-82M"
    assert _resolve_repo("kokoro-v1.0") == "hexgrad/Kokoro-82M"
    with pytest.raises(ValueError, match="unsupported Kokoro model"):
        _resolve_repo("unknown-model-xyz")


@pytest.mark.asyncio
async def test_service_emits_phone_ready_audio_frames() -> None:
    class _Executor:
        def submit(self, fn, *args, **kwargs):
            from concurrent.futures import Future

            future: Future = Future()
            future.set_result(fn(*args, **kwargs))
            return future

    class _Pipeline:
        def __init__(self) -> None:
            self.lang_code = "a"

        def __call__(self, text: str, voice: str = "af_heart", speed: float = 1.0):
            yield ("g", "p", np.zeros(2400, dtype=np.float32))

    service = PhoneAgentKokoroTTSService(voice="af_heart", sample_rate=16_000)
    service._engine = _KokoroEngine(backend=_Pipeline(), executor=_Executor(), device="cpu")
    latency: list[dict] = []
    service.set_latency_sink(latency.append)

    frames = [frame async for frame in service.run_tts("Hello from Kokoro!", "ctx-1")]

    audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
    assert len(audio) == 1
    assert audio[0].sample_rate == 16_000
    assert audio[0].num_channels == 1
    # 0.1 s at 24 kHz becomes 0.1 s at 16 kHz, two bytes a sample.
    assert len(audio[0].audio) == pytest.approx(3200, abs=64)
    assert latency[0]["stage"] == "tts_ttfa"
    assert latency[0]["provider"] == "kokoro"
    assert latency[0]["text_chars"] == len("Hello from Kokoro!")


@pytest.mark.asyncio
async def test_blank_text_synthesizes_nothing() -> None:
    service = PhoneAgentKokoroTTSService(voice="af_heart", sample_rate=16_000)

    assert [frame async for frame in service.run_tts("   ", "ctx-1")] == []
