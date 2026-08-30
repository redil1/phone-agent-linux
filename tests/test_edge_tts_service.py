"""Tests for gap-free Edge MP3 decoding and Pipecat frame conversion."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncGenerator

import numpy as np
import pytest
from phone_agent_gateway.ai_bridge.edge_tts_service import (
    EdgeTTSService,
    FFmpegMP3StreamDecoder,
    PhraseTextAggregator,
    _PrefetchedPCMStream,
    split_edge_phrases,
)
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame


def _test_mp3(duration_ms: int = 300) -> bytes:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for the Edge TTS adapter")
    samples = np.arange(24_000 * duration_ms // 1000)
    signal = (np.sin(2 * np.pi * 440 * samples / 24_000) * 12_000).astype(np.int16)
    process = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-f",
            "mp3",
            "pipe:1",
        ],
        input=signal.tobytes(),
        capture_output=True,
        check=True,
    )
    return process.stdout


@pytest.mark.asyncio
async def test_phrase_aggregator_releases_natural_bounded_chunks() -> None:
    aggregator = PhraseTextAggregator(min_chars=12, max_chars=40)
    output = []

    for token in ("This is a useful phrase, ", "followed by another short sentence."):
        output.extend([item.text async for item in aggregator.aggregate(token)])
    remaining = await aggregator.flush()
    if remaining:
        output.append(remaining.text)

    assert output == [
        "This is a useful phrase,",
        "followed by another short sentence.",
    ]


def test_prefetch_phrase_split_matches_live_boundaries() -> None:
    assert split_edge_phrases(
        "This is a useful phrase, followed by another short sentence.",
        min_chars=12,
        max_chars=40,
    ) == ["This is a useful phrase,", "followed by another short sentence."]


@pytest.mark.asyncio
async def test_incremental_decoder_ignores_arbitrary_network_boundaries() -> None:
    encoded = _test_mp3()
    decoder = FFmpegMP3StreamDecoder(16_000)
    output: list[bytes] = []

    await decoder.start()

    async def feed() -> None:
        for offset in range(0, len(encoded), 37):
            await decoder.write(encoded[offset : offset + 37])
        await decoder.close_input()

    writer = asyncio.create_task(feed())
    output = [chunk async for chunk in decoder.read()]
    await writer
    await decoder.wait()

    pcm = b"".join(output)
    assert pcm
    assert len(pcm) % 2 == 0
    assert 8_000 < len(pcm) < 14_000


class _FakeCommunicator:
    def __init__(self, chunks: list[dict], captured: dict, kwargs: dict) -> None:
        self._chunks = chunks
        captured.update(kwargs)

    async def stream(self) -> AsyncGenerator[dict, None]:
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_service_emits_16khz_mono_pcm_and_forwards_settings() -> None:
    encoded = _test_mp3()
    chunks = [
        {"type": "SentenceBoundary", "text": "Hello."},
        *(
            {"type": "audio", "data": encoded[offset : offset + 101]}
            for offset in range(0, len(encoded), 101)
        ),
    ]
    captured: dict = {}

    def factory(**kwargs):
        return _FakeCommunicator(chunks, captured, kwargs)

    service = EdgeTTSService(
        voice="en-US-EmmaMultilingualNeural",
        rate="+5%",
        sample_rate=16_000,
        communicator_factory=factory,
    )
    frames = [frame async for frame in service.run_tts("Hello there.", "context")]
    audio = [frame for frame in frames if isinstance(frame, TTSAudioRawFrame)]

    assert audio
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert all(frame.sample_rate == 16_000 and frame.num_channels == 1 for frame in audio)
    assert b"".join(frame.audio for frame in audio)
    assert captured["text"] == "Hello there."
    assert captured["rate"] == "+5%"
    assert captured["boundary"] == "SentenceBoundary"
    assert captured["connect_timeout"] == 5


@pytest.mark.asyncio
async def test_service_skips_punctuation_only_fragments() -> None:
    called = False

    def factory(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be called")

    service = EdgeTTSService(communicator_factory=factory)
    frames = [frame async for frame in service.run_tts(" - ", "context")]

    assert frames == []
    assert called is False


@pytest.mark.asyncio
async def test_service_cancellation_stops_waiting_network_and_decoder() -> None:
    entered_stream = asyncio.Event()

    class WaitingCommunicator:
        async def stream(self) -> AsyncGenerator[dict, None]:
            entered_stream.set()
            await asyncio.Event().wait()
            yield {"type": "audio", "data": b"unreachable"}

    service = EdgeTTSService(communicator_factory=lambda **_kwargs: WaitingCommunicator())

    async def consume() -> None:
        async for _frame in service.run_tts("Please wait.", "context"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered_stream.wait(), timeout=2.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_live_edge_retries_an_empty_stream_then_emits_pcm() -> None:
    encoded = _test_mp3()
    calls = 0

    def factory(**kwargs):
        nonlocal calls
        calls += 1
        chunks = [] if calls == 1 else [{"type": "audio", "data": encoded}]
        return _FakeCommunicator(chunks, {}, kwargs)

    service = EdgeTTSService(communicator_factory=factory, live_attempts=3)
    frames = [frame async for frame in service.run_tts("Bonjour.", "context")]

    assert calls == 2
    assert any(isinstance(frame, TTSAudioRawFrame) for frame in frames)
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)


@pytest.mark.asyncio
async def test_live_edge_reports_failure_only_after_all_live_attempts() -> None:
    calls = 0

    def factory(**kwargs):
        nonlocal calls
        calls += 1
        return _FakeCommunicator([], {}, kwargs)

    service = EdgeTTSService(communicator_factory=factory, live_attempts=3)
    frames = [frame async for frame in service.run_tts("Bonjour.", "context")]

    assert calls == 3
    errors = [frame for frame in frames if isinstance(frame, ErrorFrame)]
    assert len(errors) == 1
    assert errors[0].error.startswith("Edge TTS failed:")


@pytest.mark.asyncio
async def test_speculative_edge_pcm_is_reused_without_second_network_call() -> None:
    encoded = _test_mp3()
    calls = 0

    def factory(**kwargs):
        nonlocal calls
        calls += 1
        chunks = [{"type": "audio", "data": encoded}]
        return _FakeCommunicator(chunks, {}, kwargs)

    service = EdgeTTSService(communicator_factory=factory)
    await service.prefetch_text("Hello there.")
    frames = [frame async for frame in service.run_tts("Hello there.", "context")]

    assert calls == 1
    assert any(isinstance(frame, TTSAudioRawFrame) for frame in frames)


@pytest.mark.asyncio
async def test_committed_playback_attaches_to_in_progress_prefetch_stream() -> None:
    stream = _PrefetchedPCMStream()
    first_chunk_seen = asyncio.Event()
    release_second_chunk = asyncio.Event()

    async def produce() -> None:
        stream.append(b"first")
        first_chunk_seen.set()
        await release_second_chunk.wait()
        stream.append(b"second")
        stream.finish()

    producer = asyncio.create_task(produce())
    reader = stream.read()

    first = await anext(reader)
    await asyncio.wait_for(first_chunk_seen.wait(), timeout=1.0)
    assert first == b"first"
    assert producer.done() is False

    release_second_chunk.set()
    remaining = [chunk async for chunk in reader]
    await producer
    assert remaining == [b"second"]


@pytest.mark.asyncio
async def test_reflex_pcm_is_persisted_and_reloaded_without_network(tmp_path) -> None:
    encoded = _test_mp3()
    calls = 0

    def factory(**kwargs):
        nonlocal calls
        calls += 1
        return _FakeCommunicator([{"type": "audio", "data": encoded}], {}, kwargs)

    service = EdgeTTSService(
        communicator_factory=factory,
        reflex_cache_dir=tmp_path,
    )
    await service.warm_reflexes(("I see.",))
    first = service.get_reflex_pcm("I see.")

    def forbidden_factory(**_kwargs):
        raise AssertionError("persistent reflex cache must avoid the network")

    reloaded = EdgeTTSService(
        communicator_factory=forbidden_factory,
        reflex_cache_dir=tmp_path,
    )
    second = reloaded.get_reflex_pcm("I see.")

    assert calls == 1
    assert first
    assert second == first
