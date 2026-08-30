"""Telephone-audio contract tests for the local Supertonic adapter."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from phone_agent_gateway.ai_bridge.supertonic_tts_service import (
    PhoneAgentSupertonicTTSService,
    _SupertonicEngine,
    _waveform_to_pcm16,
)
from pipecat.frames.frames import TTSAudioRawFrame


class FakeBackend:
    sample_rate = 44_100
    model_name = "supertonic-3"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def get_voice_style(self, voice_name: str) -> str:
        return voice_name

    def synthesize(self, text: str, **_kwargs: object) -> tuple[np.ndarray, np.ndarray]:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("synthetic local failure")
        count = int(self.sample_rate * 0.11)
        timeline = np.arange(count, dtype=np.float32) / self.sample_rate
        waveform = (0.2 * np.sin(2 * np.pi * 220 * timeline))[None, :]
        return waveform, np.array([0.11], dtype=np.float32)


class FakeFallback:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.cleaned = False

    async def synthesize_pcm(self, text: str) -> bytes:
        self.calls.append(text)
        return (np.ones(1_600, dtype="<i2") * 100).tobytes()

    async def cleanup(self) -> None:
        self.cleaned = True


def engine(backend: FakeBackend) -> _SupertonicEngine:
    return _SupertonicEngine(backend=backend, executor=ThreadPoolExecutor(max_workers=1))


async def rendered_frames(service: PhoneAgentSupertonicTTSService, text: str):
    return [frame async for frame in service.run_tts(text, "test-context")]


def test_waveform_is_resampled_once_to_clean_16khz_pcm() -> None:
    waveform = np.linspace(-0.5, 0.5, 44_100, dtype=np.float32)[None, :]
    pcm = _waveform_to_pcm16(waveform, 44_100, 16_000)

    samples = np.frombuffer(pcm, dtype="<i2")
    assert 15_990 <= samples.size <= 16_010
    # A steep test ramp can ring slightly at the resampler boundary, but must
    # remain comfortably below clipping.
    assert np.max(np.abs(samples)) <= 20_000


@pytest.mark.asyncio
async def test_audio_obeys_phone_frame_contract() -> None:
    backend = FakeBackend()
    service = PhoneAgentSupertonicTTSService(engine=engine(backend))

    frames = await rendered_frames(service, "A clear local reply.")
    audio = [frame for frame in frames if isinstance(frame, TTSAudioRawFrame)]

    assert audio
    assert all(frame.sample_rate == 16_000 for frame in audio)
    assert all(frame.num_channels == 1 for frame in audio)
    assert all(0 < len(frame.audio) <= 640 for frame in audio)
    assert all(len(frame.audio) == 640 for frame in audio[:-1])
    assert backend.calls == ["A clear local reply."]
    await service.cleanup()


@pytest.mark.asyncio
async def test_speculative_audio_is_reused_without_second_inference() -> None:
    backend = FakeBackend()
    service = PhoneAgentSupertonicTTSService(engine=engine(backend))

    await service.prefetch_text("The answer is ready.")
    frames = await rendered_frames(service, "The answer is ready.")

    assert any(isinstance(frame, TTSAudioRawFrame) for frame in frames)
    assert backend.calls == ["The answer is ready."]
    await service.cleanup()


@pytest.mark.asyncio
async def test_edge_fallback_is_used_only_when_local_synthesis_fails() -> None:
    backend = FakeBackend(fail=True)
    fallback = FakeFallback()
    service = PhoneAgentSupertonicTTSService(
        engine=engine(backend), fallback_renderer=fallback
    )

    frames = await rendered_frames(service, "Keep the call alive.")

    assert any(isinstance(frame, TTSAudioRawFrame) for frame in frames)
    assert fallback.calls == ["Keep the call alive."]
    await service.cleanup()
    assert fallback.cleaned is True


@pytest.mark.asyncio
async def test_reflex_cache_survives_service_restart(tmp_path: Path) -> None:
    backend = FakeBackend()
    first = PhoneAgentSupertonicTTSService(
        engine=engine(backend), reflex_cache_dir=tmp_path
    )
    await first.warm_reflexes(("I understand.",))
    expected = first.get_reflex_pcm("I understand.")
    await first.cleanup()

    second = PhoneAgentSupertonicTTSService(
        engine=engine(FakeBackend()), reflex_cache_dir=tmp_path
    )
    assert expected
    assert second.get_reflex_pcm("I understand.") == expected
    await second.cleanup()
