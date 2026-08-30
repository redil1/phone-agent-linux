"""Offline tests for the Google GenAI streaming TTS adapter."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

from phone_agent_gateway.ai_bridge.google_genai_tts_service import (
    GoogleGenAITTSService,
    StreamingPCMResampler,
)


class _FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.closed = True


class _FakeModels:
    def __init__(self, stream: _FakeStream) -> None:
        self.stream = stream
        self.request: dict[str, Any] = {}

    async def generate_content_stream(self, **kwargs: Any) -> _FakeStream:
        self.request = kwargs
        return self.stream


class _SequenceModels:
    def __init__(self, streams: list[Any]) -> None:
        self.streams = list(streams)
        self.requests: list[dict[str, Any]] = []

    async def generate_content_stream(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return self.streams.pop(0)


class _HangingStream:
    def __init__(self, first_chunk: Any | None = None) -> None:
        self.first_chunk = first_chunk
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.first_chunk is not None:
            chunk = self.first_chunk
            self.first_chunk = None
            return chunk
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class _FailingModels:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0
        self.model_fallback_calls = 0

    async def generate_content_stream(self, **_kwargs: Any) -> Any:
        self.calls += 1
        raise self.error

    async def generate_content(self, **_kwargs: Any) -> Any:
        self.model_fallback_calls += 1
        raise self.error


class _QuotaThenGeminiFallbackModels:
    def __init__(self, audio: bytes) -> None:
        self.audio = audio
        self.primary_calls = 0
        self.fallback_requests: list[dict[str, Any]] = []

    async def generate_content_stream(self, **_kwargs: Any) -> Any:
        self.primary_calls += 1
        raise RuntimeError(
            "429 Too Many Requests: RESOURCE_EXHAUSTED quota exceeded "
            "generate_requests_per_model_per_day limit: 100"
        )

    async def generate_content(self, **kwargs: Any) -> Any:
        self.fallback_requests.append(kwargs)
        part = SimpleNamespace(inline_data=SimpleNamespace(data=self.audio))
        content = SimpleNamespace(parts=[part])
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


class _FallbackTTS:
    def __init__(self) -> None:
        self.calls = 0
        self.cleaned = False

    async def run_tts(self, _text: str, context_id: str):
        self.calls += 1
        yield TTSAudioRawFrame(
            audio=b"\x01\x00" * 320,
            sample_rate=16_000,
            num_channels=1,
            context_id=context_id,
        )

    async def cleanup(self) -> None:
        self.cleaned = True


@pytest.mark.asyncio
async def test_google_tts_streams_resampled_pcm_without_network() -> None:
    samples = (np.sin(np.linspace(0, 20, 2400)) * 12_000).astype(np.int16).tobytes()
    parts = [samples[:1777], samples[1777:]]
    chunks = [
        SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=part))])
        for part in parts
    ]
    stream = _FakeStream(chunks)
    models = _FakeModels(stream)
    fake_aio = SimpleNamespace(models=models, aclose=_async_noop)
    service = GoogleGenAITTSService(api_key="test", sample_rate=16_000)
    service._client = SimpleNamespace(aio=fake_aio)

    frames = [frame async for frame in service.run_tts("Salam", "call-1")]
    audio = [frame for frame in frames if isinstance(frame, TTSAudioRawFrame)]

    assert audio
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert all(frame.sample_rate == 16_000 and frame.num_channels == 1 for frame in audio)
    assert 3000 <= len(b"".join(frame.audio for frame in audio)) <= 3400
    assert models.request["model"] == "gemini-3.1-flash-tts-preview"
    assert stream.closed is True
    await service.cleanup()


async def _async_noop() -> None:
    return None


def test_streaming_resampler_handles_odd_api_boundaries() -> None:
    raw = np.arange(2400, dtype=np.int16).tobytes()
    resampler = StreamingPCMResampler(24_000, 16_000)
    output = b"".join(
        [
            resampler.push(raw[:301]),
            resampler.push(raw[301:2777]),
            resampler.push(raw[2777:]),
            resampler.push(b"", final=True),
        ]
    )
    assert len(output) == 3200


def test_google_tts_prompt_separates_scene_context_and_verbatim_transcript() -> None:
    service = GoogleGenAITTSService(
        api_key="test",
        scene="A quiet English-language customer call.",
        sample_context="Adam continues warmly at a natural conversational pace.",
    )

    prompt = service._format_prompt("This is the exact answer, number 42.")

    assert "## THE SCENE\nA quiet English-language customer call." in prompt
    assert "### SAMPLE CONTEXT\nAdam continues warmly" in prompt
    assert "#### TRANSCRIPT TO SPEAK VERBATIM\nThis is the exact answer, number 42." in prompt
    assert "never speak these instructions" in prompt


@pytest.mark.asyncio
async def test_google_tts_retries_same_model_after_first_audio_stall() -> None:
    samples = (np.sin(np.linspace(0, 10, 1200)) * 10_000).astype(np.int16).tobytes()
    stalled = _HangingStream()
    recovered = _FakeStream(
        [SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=samples))])]
    )
    models = _SequenceModels([stalled, recovered])
    service = GoogleGenAITTSService(
        api_key="test",
        sample_rate=16_000,
        first_audio_timeout_secs=0.05,
        chunk_timeout_secs=0.05,
        total_timeout_secs=0.2,
        max_attempts=2,
    )
    service._client = SimpleNamespace(aio=SimpleNamespace(models=models, aclose=_async_noop))

    async def keep_fake_client() -> None:
        return None

    service._recreate_client = keep_fake_client  # type: ignore[method-assign]
    started = time.monotonic()
    frames = [frame async for frame in service.run_tts("Recover me", "call-retry")]

    assert time.monotonic() - started < 0.5
    assert any(isinstance(frame, TTSAudioRawFrame) for frame in frames)
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert len(models.requests) == 2
    assert {request["model"] for request in models.requests} == {"gemini-3.1-flash-tts-preview"}
    assert stalled.closed is True


@pytest.mark.asyncio
async def test_partial_audio_stall_fails_fast_without_poisoning_next_turn() -> None:
    samples = (np.sin(np.linspace(0, 10, 1200)) * 10_000).astype(np.int16).tobytes()
    audio_chunk = SimpleNamespace(
        parts=[SimpleNamespace(inline_data=SimpleNamespace(data=samples))]
    )
    stalled_after_audio = _HangingStream(first_chunk=audio_chunk)
    next_turn = _FakeStream([audio_chunk])
    models = _SequenceModels([stalled_after_audio, next_turn])
    service = GoogleGenAITTSService(
        api_key="test",
        sample_rate=16_000,
        first_audio_timeout_secs=0.05,
        chunk_timeout_secs=0.05,
        total_timeout_secs=0.2,
        max_attempts=2,
    )
    service._client = SimpleNamespace(aio=SimpleNamespace(models=models, aclose=_async_noop))

    async def keep_fake_client() -> None:
        return None

    service._recreate_client = keep_fake_client  # type: ignore[method-assign]
    failed_frames = [frame async for frame in service.run_tts("First", "call-first")]
    recovered_frames = [frame async for frame in service.run_tts("Second", "call-second")]

    assert any(isinstance(frame, TTSAudioRawFrame) for frame in failed_frames)
    assert any(isinstance(frame, ErrorFrame) for frame in failed_frames)
    assert any(isinstance(frame, TTSAudioRawFrame) for frame in recovered_frames)
    assert not any(isinstance(frame, ErrorFrame) for frame in recovered_frames)
    assert len(models.requests) == 2


@pytest.mark.asyncio
async def test_google_daily_quota_uses_edge_only_per_turn_when_both_gemini_models_fail() -> None:
    models = _FailingModels(
        RuntimeError(
            "429 Too Many Requests: RESOURCE_EXHAUSTED quota exceeded "
            "generate_requests_per_model_per_day limit: 100"
        )
    )
    fallback = _FallbackTTS()
    service = GoogleGenAITTSService(
        api_key="test",
        sample_rate=16_000,
        max_attempts=2,
        fallback_service=fallback,
    )
    service._client = SimpleNamespace(aio=SimpleNamespace(models=models, aclose=_async_noop))

    first = [frame async for frame in service.run_tts("First", "quota-first")]
    second = [frame async for frame in service.run_tts("Second", "quota-second")]

    assert models.calls == 1
    assert models.model_fallback_calls == 2
    assert fallback.calls == 2
    assert all(
        any(isinstance(frame, TTSAudioRawFrame) for frame in turn) for turn in (first, second)
    )
    assert not any(isinstance(frame, ErrorFrame) for frame in [*first, *second])
    await service.cleanup()
    assert fallback.cleaned is True


@pytest.mark.asyncio
async def test_google_quota_preserves_voice_profile_with_second_gemini_model() -> None:
    samples = (np.sin(np.linspace(0, 12, 2400)) * 10_000).astype(np.int16).tobytes()
    models = _QuotaThenGeminiFallbackModels(samples)
    edge = _FallbackTTS()
    service = GoogleGenAITTSService(
        api_key="test",
        sample_rate=16_000,
        voice="Aoede",
        scene="Une conversation téléphonique en français.",
        sample_context="Une voix française native et naturelle.",
        max_attempts=2,
        fallback_service=edge,
    )
    service._client = SimpleNamespace(aio=SimpleNamespace(models=models, aclose=_async_noop))

    first = [frame async for frame in service.run_tts("Bonjour.", "gemini-first")]
    second = [frame async for frame in service.run_tts("Très bien.", "gemini-second")]

    assert models.primary_calls == 1
    assert len(models.fallback_requests) == 2
    assert {request["model"] for request in models.fallback_requests} == {
        "gemini-2.5-flash-preview-tts"
    }
    assert all(
        "française native" in request["contents"][0].parts[0].text
        for request in models.fallback_requests
    )
    assert edge.calls == 0
    assert all(
        any(isinstance(frame, TTSAudioRawFrame) for frame in turn) for turn in (first, second)
    )
    assert not any(isinstance(frame, ErrorFrame) for frame in [*first, *second])


@pytest.mark.asyncio
async def test_selected_gemini_25_uses_nonstreaming_api_directly() -> None:
    samples = (np.sin(np.linspace(0, 12, 2400)) * 10_000).astype(np.int16).tobytes()
    models = _QuotaThenGeminiFallbackModels(samples)
    edge = _FallbackTTS()
    service = GoogleGenAITTSService(
        api_key="test",
        model="gemini-2.5-flash-preview-tts",
        sample_rate=16_000,
        voice="Aoede",
        fallback_service=edge,
    )
    service._client = SimpleNamespace(aio=SimpleNamespace(models=models, aclose=_async_noop))

    frames = [frame async for frame in service.run_tts("Bonjour.", "selected-25")]

    assert models.primary_calls == 0
    assert len(models.fallback_requests) == 1
    assert models.fallback_requests[0]["model"] == "gemini-2.5-flash-preview-tts"
    assert edge.calls == 0
    assert any(isinstance(frame, TTSAudioRawFrame) for frame in frames)
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
