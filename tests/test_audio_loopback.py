"""Offline tests for the Pipecat phone transport boundary."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from pipecat.frames.frames import OutputAudioRawFrame

import phone_agent_gateway.ai_bridge.pipecat_transport as transport_module
from phone_agent_gateway.ai_bridge.media_protocol import FrameDirection, FrameKind, MediaFrame
from phone_agent_gateway.ai_bridge.pipecat_transport import (
    AudioWriteResult,
    PhoneAgentTransport,
    PhoneAgentTransportParams,
    PhoneAudioEndFrame,
)
from phone_agent_gateway.ai_bridge.session import SessionPhase


def active_transport() -> PhoneAgentTransport:
    transport = PhoneAgentTransport(
        PhoneAgentTransportParams(
            audio_in_sample_rate=16_000,
            audio_out_sample_rate=16_000,
        )
    )
    transport.session.set_phase(SessionPhase.CONNECTING)
    transport.session.set_phase(SessionPhase.ACTIVE)
    return transport


def test_default_ingress_queue_absorbs_half_second_jitter_burst() -> None:
    params = PhoneAgentTransportParams()

    assert params.input_queue_frames == 25
    assert params.input_queue_frames * params.frame_ms == 500


@pytest.mark.asyncio
async def test_output_preserves_clean_tts_pcm_without_chunk_dsp() -> None:
    transport = active_transport()
    sent_frames: list[bytes] = []
    transport.set_tx_handler(lambda payload, _generation, _sequence: sent_frames.append(payload))
    samples = (12000, -9000, 26000, -24000) * 80
    audio = b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)

    written = await transport.output().write_audio_frame(
        OutputAudioRawFrame(audio=audio, sample_rate=16_000, num_channels=1)
    )

    assert written is True
    assert sent_frames == [audio]


def test_input_reassembles_tcp_chunks_into_exact_frames() -> None:
    transport = active_transport()

    transport.feed_phone_audio(b"\x01" * 300)
    assert len(transport.input()._pending) == 0

    transport.feed_phone_audio(b"\x02" * 340)
    assert list(transport.input()._pending) == [b"\x01" * 300 + b"\x02" * 340]


@pytest.mark.asyncio
async def test_output_routes_audio_and_accounts_generation() -> None:
    transport = active_transport()
    sent_frames: list[bytes] = []
    transport.set_tx_handler(lambda payload, _generation, _sequence: sent_frames.append(payload))
    audio = b"\x00" * 640

    written = await transport.output().write_audio_frame(
        OutputAudioRawFrame(audio=audio, sample_rate=16_000, num_channels=1)
    )

    assert written is True
    assert sent_frames == [audio]
    assert transport.session.metrics.output_frames == 1


@pytest.mark.asyncio
async def test_output_bursts_ready_audio_without_python_clock_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = active_transport()
    sent: list[bytes] = []

    async def unexpected_sleep(_delay: float) -> None:
        raise AssertionError("ready phone audio must not be paced by the Python event loop")

    monkeypatch.setattr(transport_module.asyncio, "sleep", unexpected_sleep)
    transport.set_tx_handler(
        lambda payload, _generation, _sequence: sent.append(payload)
    )

    written = await transport.output().write_audio_frame(
        OutputAudioRawFrame(audio=b"\x01\x00" * 320 * 5, sample_rate=16_000, num_channels=1)
    )

    assert written is True
    assert sent == [b"\x01\x00" * 320] * 5


@pytest.mark.asyncio
async def test_audio_end_marker_follows_the_last_pcm_sequence() -> None:
    transport = active_transport()
    sent: list[tuple[str, int, int]] = []
    transport.set_tx_handler(
        lambda _payload, generation, sequence: sent.append(("audio", generation, sequence))
    )
    transport.set_audio_end_handler(
        lambda generation, sequence: sent.append(("end", generation, sequence))
    )

    await transport.output().write_audio_frame(
        OutputAudioRawFrame(audio=b"\x01\x00" * 320, sample_rate=16_000, num_channels=1)
    )
    await transport.output().write_transport_frame(PhoneAudioEndFrame())

    assert sent == [("audio", 1, 0), ("end", 1, 1)]
    assert transport.output().audio_end_epoch == 1
    assert await transport.output().wait_for_audio_end(0) == (1, 1)


@pytest.mark.asyncio
async def test_interruption_flushes_and_advances_generation() -> None:
    transport = active_transport()
    flushes: list[bool] = []
    transport.set_flush_handler(lambda _advance: flushes.append(True))
    before = transport.session.generation_id

    advance = await transport.coordinator.interrupt(
        "test_barge_in", transport.output()._flush_phone
    )

    assert advance.cancelled_generation == before
    assert advance.next_generation == before + 1
    assert transport.session.generation_id == before + 1
    assert flushes == [True]


@pytest.mark.asyncio
async def test_inflight_old_generation_write_is_normal_cancellation() -> None:
    transport = active_transport()
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    async def interrupted_write(_payload: bytes, _generation: int, _sequence: int) -> None:
        write_started.set()
        await release_write.wait()
        raise RuntimeError("old generation rejected")

    transport.set_tx_handler(interrupted_write)
    write_task = asyncio.create_task(
        transport.output().write_audio_frame_result(
            OutputAudioRawFrame(audio=b"\x01\x00" * 320, sample_rate=16_000, num_channels=1)
        )
    )
    await write_started.wait()
    transport.session.interrupt("caller_barge_in")
    release_write.set()

    assert await write_task is AudioWriteResult.CANCELLED
    assert transport.session.metrics.dropped_output_frames == 0


@pytest.mark.asyncio
async def test_delivered_phone_audio_is_exposed_as_echo_reference() -> None:
    transport = active_transport()
    delivered: list[bytes] = []
    transport.add_output_audio_listener(delivered.append)
    transport.set_tx_handler(lambda _payload, _generation, _sequence: None)
    payload = b"\x12\x00" * 320

    result = await transport.output().write_audio_frame_result(
        OutputAudioRawFrame(audio=payload, sample_rate=16_000, num_channels=1)
    )

    assert result is AudioWriteResult.DELIVERED
    assert delivered == [payload]


def test_framed_input_rejects_wrong_call_and_stale_generation() -> None:
    transport = active_transport()
    current = MediaFrame(
        kind=FrameKind.AUDIO,
        direction=FrameDirection.PHONE_TO_MAC,
        call_id=transport.session.call_id,
        generation_id=transport.session.generation_id,
        sequence=0,
        monotonic_ns=time.monotonic_ns(),
        payload=b"\x00" * 640,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
    )

    transport.feed_phone_frame(replace(current, call_id=uuid4()))
    transport.feed_phone_frame(replace(current, generation_id=current.generation_id + 1))
    transport.feed_phone_frame(current)

    assert list(transport.input()._pending) == [current.payload]
    assert transport.session.metrics.stale_input_frames == 2
