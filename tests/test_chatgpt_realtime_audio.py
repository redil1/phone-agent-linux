"""Unit tests for PhoneMediaStreamTrack audio conversion and resampling."""

from __future__ import annotations

import asyncio
from pathlib import Path

import av
import numpy as np
import pytest
import soxr
from phone_agent_gateway.ai_bridge.chatgpt_realtime_pipeline import (
    PHONE_CHUNK_BYTES,
    PHONE_SAMPLE_RATE,
    SAMPLES_PER_FRAME_48K,
    WEB_RTC_SAMPLE_RATE,
    ChatGPTRealtimePipeline,
    PhoneMediaStreamTrack,
)
from phone_agent_gateway.ai_bridge.pipecat_transport import PhoneAgentTransport
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig, RuntimeConfig
from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase


@pytest.mark.asyncio
async def test_phone_media_stream_track_resampling():
    track = PhoneMediaStreamTrack()
    try:
        # Generate 20ms of 16kHz sine wave audio (320 samples = 640 bytes)
        t = np.linspace(0, 0.02, 320, endpoint=False)
        sine_16k = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        pcm_16k = sine_16k.tobytes()
        assert len(pcm_16k) == PHONE_CHUNK_BYTES

        # Push to track
        track.enable_input()
        track.push_pcm_frame(pcm_16k)

        # Receive resampled 48kHz AudioFrame
        frame = await track.recv()
        assert frame.sample_rate == WEB_RTC_SAMPLE_RATE
        assert frame.format.name == "s16"
        assert frame.layout.name == "mono"

        # Check sample count: 20ms at 48kHz = 960 samples
        audio_array = frame.to_ndarray()
        assert audio_array.shape == (1, SAMPLES_PER_FRAME_48K)
        assert np.max(np.abs(audio_array)) > 5000  # Verify non-silent waveform was preserved
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_quiet_caller_audio_is_never_erased_by_local_activity_detection():
    """Regression: the former fixed RMS gate deleted quiet initial consonants."""

    track = PhoneMediaStreamTrack()
    try:
        track.enable_input()
        quiet = (np.ones(320, dtype=np.int16) * 120).tobytes()
        track.push_pcm_frame(quiet)

        rendered = (await track.recv()).to_ndarray().reshape(-1)

        assert np.max(np.abs(rendered)) >= 80
        assert track.speech_active is True
        quality = track.quality_snapshot()
        assert quality["caller_input_frames"] == 1
        assert quality["caller_input_peak"] == 120
        assert quality["caller_input_queue_drops"] == 0
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_phone_media_track_drops_caller_audio_until_persona_gate_opens():
    track = PhoneMediaStreamTrack()
    try:
        loud_audio = (np.ones(320, dtype=np.int16) * 12000).tobytes()
        track.push_pcm_frame(loud_audio)
        before = (await track.recv()).to_ndarray()
        assert np.all(before == 0)

        track.enable_input()
        track.push_pcm_frame(loud_audio)
        after = (await track.recv()).to_ndarray()
        assert np.max(np.abs(after)) > 5000
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_phone_media_stream_track_silence_on_empty():
    track = PhoneMediaStreamTrack()
    try:
        # Without pushing audio, recv should produce silence with proper dimensions
        frame = await track.recv()
        assert frame.sample_rate == WEB_RTC_SAMPLE_RATE
        audio_array = frame.to_ndarray()
        assert audio_array.shape == (1, SAMPLES_PER_FRAME_48K)
        assert np.all(audio_array == 0)
    finally:
        track.stop()


def test_bidirectional_resampling_roundtrip():
    # Generate 16kHz test tone
    duration_sec = 0.1
    samples_in = int(PHONE_SAMPLE_RATE * duration_sec)
    t = np.linspace(0, duration_sec, samples_in, endpoint=False)
    original_16k = (np.sin(2 * np.pi * 1000 * t) * 20000).astype(np.float32)

    # 16kHz -> 48kHz
    resampled_48k = soxr.resample(
        original_16k, PHONE_SAMPLE_RATE, WEB_RTC_SAMPLE_RATE, quality="HQ"
    )
    assert len(resampled_48k) == int(WEB_RTC_SAMPLE_RATE * duration_sec)

    # 48kHz -> 16kHz
    roundtrip_16k = soxr.resample(
        resampled_48k, WEB_RTC_SAMPLE_RATE, PHONE_SAMPLE_RATE, quality="HQ"
    )
    assert len(roundtrip_16k) == samples_in

    # Check signal correlation is high (> 0.99)
    correlation = np.corrcoef(original_16k, roundtrip_16k)[0, 1]
    assert correlation > 0.99


@pytest.mark.asyncio
async def test_packed_stereo_realtime_stream_keeps_exact_phone_timing():
    """Regression: packed stereo is shaped (1, 1920), not (2, 960)."""

    providers = ProviderConfig(
        pipeline_mode="s2s_chatgpt_realtime",
        chatgpt_realtime_voice="alloy",
        stt_language="en-US",
    )
    config = RuntimeConfig(
        device_id="test",
        control_host="127.0.0.1",
        control_port=8765,
        protocol_control_port=8768,
        rx_port=8766,
        tx_port=8767,
        sample_rate=16000,
        frame_ms=20,
        input_queue_frames=25,
        auto_answer=False,
        record_calls=False,
        memory_enabled=False,
        task_id="iptv_subscription_sales",
        event_stream_enabled=False,
        voice_lock_path=Path("/tmp/test-phone-agent.lock"),
        system_prompt="",
        link_authentication_key=b"0" * 32,
        providers=providers,
    )
    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    session.set_phase(SessionPhase.ACTIVE)
    pipeline = ChatGPTRealtimePipeline(PhoneAgentTransport(session=session), config)
    state = pipeline._response_state({"response": {"id": "stereo-test"}}, {})
    pipeline._remote_audio_response_key = state.key
    pipeline._running = True

    t = np.linspace(0, 0.02, 960, endpoint=False)
    mono = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
    packed = np.column_stack((mono, mono)).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(packed, format="s16", layout="stereo")
    frame.sample_rate = WEB_RTC_SAMPLE_RATE

    second_frame = av.AudioFrame.from_ndarray(packed, format="s16", layout="stereo")
    second_frame.sample_rate = WEB_RTC_SAMPLE_RATE

    class TwoFrameTrack:
        def __init__(self):
            self.frames = [frame, second_frame]

        async def recv(self):
            if self.frames:
                return self.frames.pop(0)
            await asyncio.Event().wait()

    pump = asyncio.create_task(pipeline._pump_remote_audio_to_phone(TwoFrameTrack()))
    try:
        item = await asyncio.wait_for(pipeline._phone_audio_queue.get(), timeout=1.0)
        assert item.pcm is not None
        assert len(item.pcm) == PHONE_CHUNK_BYTES
        phone_samples = np.frombuffer(item.pcm, dtype=np.int16)
        assert len(phone_samples) == 320
        assert np.max(np.abs(phone_samples)) > 5000
        assert pipeline._phone_audio_queue.empty()
        assert len(pipeline._remote_pcm_accumulator) < PHONE_CHUNK_BYTES
    finally:
        pipeline._running = False
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


@pytest.mark.asyncio
async def test_pause_after_audio_done_does_not_drop_sentence_final_words():
    """A natural sentence pause is audio content, not a playback boundary."""

    providers = ProviderConfig(
        pipeline_mode="s2s_chatgpt_realtime",
        chatgpt_realtime_voice="alloy",
        stt_language="en-US",
    )
    config = RuntimeConfig(
        device_id="test",
        control_host="127.0.0.1",
        control_port=8765,
        protocol_control_port=8768,
        rx_port=8766,
        tx_port=8767,
        sample_rate=16000,
        frame_ms=20,
        input_queue_frames=25,
        auto_answer=False,
        record_calls=False,
        memory_enabled=False,
        task_id="iptv_subscription_sales",
        event_stream_enabled=False,
        voice_lock_path=Path("/tmp/test-phone-agent.lock"),
        system_prompt="",
        link_authentication_key=b"0" * 32,
        providers=providers,
    )
    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    session.set_phase(SessionPhase.ACTIVE)
    pipeline = ChatGPTRealtimePipeline(PhoneAgentTransport(session=session), config)
    pipeline._running = True

    response_id = "sentence-pause-test"
    await pipeline._handle_dc_message(
        '{"type":"response.created","response":{"id":"sentence-pause-test"}}'
    )
    await pipeline._handle_dc_message(
        '{"type":"response.output_audio.done","response_id":"sentence-pause-test"}'
    )
    state = pipeline._responses[response_id]

    def audio_frame(samples: np.ndarray) -> av.AudioFrame:
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = WEB_RTC_SAMPLE_RATE
        return frame

    t = np.linspace(0, 0.02, SAMPLES_PER_FRAME_48K, endpoint=False)
    first_words = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
    final_words = (np.sin(2 * np.pi * 880 * t) * 18000).astype(np.int16)
    silence = np.zeros(SAMPLES_PER_FRAME_48K, dtype=np.int16)
    frames = (
        [audio_frame(first_words) for _ in range(2)]
        + [audio_frame(silence) for _ in range(8)]
        + [audio_frame(final_words) for _ in range(2)]
    )

    class PausedSentenceTrack:
        def __init__(self):
            self.frames = frames

        async def recv(self):
            if self.frames:
                return self.frames.pop(0)
            await asyncio.Event().wait()

    track = PausedSentenceTrack()
    pump = asyncio.create_task(pipeline._pump_remote_audio_to_phone(track))
    try:
        for _ in range(100):
            if not track.frames and pipeline._remote_audio_frames >= 10:
                break
            await asyncio.sleep(0.01)

        assert not track.frames
        assert pipeline._remote_audio_frames >= 10
        assert not state.output_buffer_terminal.is_set()

        queued_audio = []
        while not pipeline._phone_audio_queue.empty():
            item = pipeline._phone_audio_queue.get_nowait()
            assert item.pcm is not None, "audio.done must not close phone playback"
            queued_audio.append(item.pcm)
        rendered = np.frombuffer(b"".join(queued_audio), dtype=np.int16)
        assert np.max(np.abs(rendered[-640:])) > 5000

        await pipeline._handle_dc_message(
            '{"type":"output_audio_buffer.stopped","response_id":"sentence-pause-test"}'
        )
        while True:
            item = await asyncio.wait_for(pipeline._phone_audio_queue.get(), timeout=1.0)
            if item.pcm is None:
                break

        assert state.output_buffer_stopped
    finally:
        pipeline._running = False
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        await pipeline.close()
