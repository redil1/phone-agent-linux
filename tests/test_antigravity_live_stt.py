"""Unit tests for Antigravity Live STT Service and Connect stream codec."""

from __future__ import annotations

import asyncio
import json
import struct
import time
from typing import Any

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from phone_agent_gateway.ai_bridge.antigravity_live_stt import (
    AntigravityLiveSTTService,
    _StreamConn,
)
from phone_agent_gateway.ai_bridge.production_pipeline import create_provider_services
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig


def test_runtime_config_accepts_antigravity_live() -> None:
    config = ProviderConfig(stt_provider="antigravity_live", tts_provider="edge_tts")
    config.validate(require_credentials=True)
    assert config.stt_provider == "antigravity_live"
    assert config.antigravity_live_chunk_ms == 200
    assert config.antigravity_live_endpoint_ms == 900
    assert config.antigravity_live_incomplete_endpoint_ms == 1500


def test_english_pipeline_uses_language_locked_context_without_changing_model() -> None:
    config = ProviderConfig(
        stt_provider="antigravity_live",
        stt_model="google-live-bridge",
        stt_language="en-US",
        tts_provider="edge_tts",
    )

    services = create_provider_services(config, 16_000)

    assert services.stt._context_bias
    assert "primarily in English" in services.stt._context_bias
    assert "never translate" in services.stt._context_bias
    assert "complete French sentence" in services.stt._context_bias
    assert config.stt_model == "google-live-bridge"


def test_french_pipeline_uses_french_first_context() -> None:
    config = ProviderConfig(
        stt_provider="antigravity_live",
        stt_model="google-live-bridge",
        stt_language="fr-FR",
        tts_provider="edge_tts",
    )

    services = create_provider_services(config, 16_000)

    assert "principalement en français" in services.stt._context_bias
    assert "sans les traduire" in services.stt._context_bias


def _build_connect_envelope(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload).encode()
    return b"\x00" + struct.pack(">I", len(data)) + data


def _build_chunked_body(envelopes: list[bytes]) -> bytes:
    chunks = bytearray()
    for env in envelopes:
        chunks.extend(f"{len(env):X}\r\n".encode())
        chunks.extend(env)
        chunks.extend(b"\r\n")
    chunks.extend(b"0\r\n\r\n")
    return bytes(chunks)


class _MockSocket:
    """Mock socket simulating HTTP/1.1 chunked response over TLS."""

    def __init__(self, raw_bytes: bytes) -> None:
        self._raw = bytearray(raw_bytes)
        self.closed = False

    def recv(self, bufsize: int) -> bytes:
        if not self._raw:
            return b""
        actual = min(len(self._raw), bufsize)
        data = bytes(self._raw[:actual])
        del self._raw[:actual]
        return data

    def settimeout(self, timeout: float) -> None:
        pass

    def shutdown(self, how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_stream_conn_dechunks_connect_envelopes() -> None:
    env1 = _build_connect_envelope({"ready": {"sessionId": "test-123"}})
    env2 = _build_connect_envelope({"transcription": {"text": "hello", "isFinal": False}})
    env3 = _build_connect_envelope({"transcription": {"text": "hello world", "isFinal": True}})

    http_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/connect+json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + _build_chunked_body([env1, env2, env3])
    )

    sock = _MockSocket(http_response)
    conn = _StreamConn(sock)  # type: ignore[arg-type]

    assert conn.status == 200
    flag1, p1 = conn.read_envelope()
    assert flag1 == 0
    assert json.loads(p1) == {"ready": {"sessionId": "test-123"}}

    flag2, p2 = conn.read_envelope()
    assert flag2 == 0
    assert json.loads(p2) == {"transcription": {"text": "hello", "isFinal": False}}

    flag3, p3 = conn.read_envelope()
    assert flag3 == 0
    assert json.loads(p3) == {"transcription": {"text": "hello world", "isFinal": True}}

    flag4, p4 = conn.read_envelope()
    assert flag4 is None
    assert p4 is None
    conn.close()
    assert sock.closed


@pytest.mark.asyncio
async def test_antigravity_live_stt_frame_flow() -> None:
    service = AntigravityLiveSTTService(
        sample_rate=16_000,
        chunk_duration_ms=50,  # 50ms chunks = 1600 bytes
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )

    env_ready = _build_connect_envelope({"ready": {"sessionId": "session-abc-123"}})
    env_partial = _build_connect_envelope({"transcription": {"text": "testing", "isFinal": False}})
    env_final = _build_connect_envelope(
        {"transcription": {"text": "testing one two", "isFinal": True}}
    )

    http_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/connect+json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        + _build_chunked_body(
            [env_ready, env_partial, env_final, _build_connect_envelope({"complete": {}})]
        )
    )

    mock_sock = _MockSocket(http_response)
    mock_conn = _StreamConn(mock_sock)  # type: ignore[arg-type]

    # Patch session start
    service._stream = mock_conn
    service._session_id = "session-abc-123"
    service._reader_task = asyncio.create_task(service._stream_reader_loop())

    sent_chunks: list[dict[str, Any]] = []

    async def mock_send(chunk: bytes, seq: int) -> None:
        sent_chunks.append({"bytes": len(chunk), "seq": seq})

    service._send_chunk = mock_send  # type: ignore[method-assign]

    pushed_frames: list[Any] = []

    async def capture_push(
        frame: Any, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        pushed_frames.append(frame)

    service.push_frame = capture_push  # type: ignore[method-assign]

    # Feed 1600 bytes (50ms of audio)
    audio_frame = InputAudioRawFrame(
        audio=b"\x00\x01" * 800,
        sample_rate=16_000,
        num_channels=1,
    )
    await service.process_frame(audio_frame, FrameDirection.DOWNSTREAM)

    assert len(sent_chunks) == 1
    assert sent_chunks[0]["bytes"] == 1600
    assert sent_chunks[0]["seq"] == 0

    # Wait for reader task to process stream envelopes
    await asyncio.sleep(0.1)

    types = [type(f) for f in pushed_frames]
    assert UserStartedSpeakingFrame in types
    assert InterimTranscriptionFrame in types
    assert TranscriptionFrame in types
    assert UserStoppedSpeakingFrame in types

    # Verify transcript text
    interim = next(f for f in pushed_frames if isinstance(f, InterimTranscriptionFrame))
    assert interim.text == "testing"

    final = next(f for f in pushed_frames if isinstance(f, TranscriptionFrame))
    assert final.text == "testing one two"

    # Cleanup
    await service._close_session()
    assert service._stream is None


@pytest.mark.asyncio
async def test_antigravity_live_stt_silence_watchdog_endpointing() -> None:
    service = AntigravityLiveSTTService(
        sample_rate=16_000,
        chunk_duration_ms=50,
        silence_endpoint_ms=100,  # 100ms for fast test
        fallback_endpoint_ms=100,
        transcript_stability_ms=20,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )

    # Server sends ONLY interim partials (never sends isFinal: true)
    env_ready = _build_connect_envelope({"ready": {"sessionId": "session-xyz-456"}})
    env_partial = _build_connect_envelope(
        {"transcription": {"text": "hello AI friend", "isFinal": False}}
    )

    http_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/connect+json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + _build_chunked_body([env_ready, env_partial])
    )

    mock_sock = _MockSocket(http_response)
    mock_conn = _StreamConn(mock_sock)  # type: ignore[arg-type]

    service._stream = mock_conn
    service._session_id = "session-xyz-456"
    service._last_speech_at = time.monotonic()
    service._reader_task = asyncio.create_task(service._stream_reader_loop())
    service._watchdog_task = asyncio.create_task(service._silence_watchdog_loop())

    pushed_frames: list[Any] = []

    async def capture_push(
        frame: Any, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        pushed_frames.append(frame)

    service.push_frame = capture_push  # type: ignore[method-assign]

    # Process partials from stream
    await asyncio.sleep(0.05)
    # The watchdog will trigger after 100ms
    await asyncio.sleep(0.12)

    types = [type(f) for f in pushed_frames]
    assert TranscriptionFrame in types
    assert UserStoppedSpeakingFrame in types

    final = next(f for f in pushed_frames if isinstance(f, TranscriptionFrame))
    assert final.text == "hello AI friend"

    await service._close_session()
    assert service._stream is None


@pytest.mark.asyncio
async def test_first_interim_does_not_immediately_interrupt_bot_audio() -> None:
    service = AntigravityLiveSTTService(
        barge_in_min_ms=250,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    env_partial = _build_connect_envelope(
        {"transcription": {"text": "brief unstable partial", "isFinal": False}}
    )
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/connect+json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + _build_chunked_body([env_partial, _build_connect_envelope({"complete": {}})])
    )
    service._stream = _StreamConn(_MockSocket(response))  # type: ignore[arg-type]
    service._session_id = "session-partial"
    pushed: list[Any] = []

    async def capture(frame: Any, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pushed.append(frame)

    service.push_frame = capture  # type: ignore[method-assign]
    await service._stream_reader_loop()

    assert any(isinstance(frame, InterimTranscriptionFrame) for frame in pushed)
    assert not any(isinstance(frame, UserStartedSpeakingFrame) for frame in pushed)
    service._session_id = None
    await service._close_session()


@pytest.mark.asyncio
async def test_final_matching_last_interim_is_still_committed() -> None:
    service = AntigravityLiveSTTService(
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    partial = {"transcription": {"text": "same words", "isFinal": False}}
    final = {"transcription": {"text": "same words", "isFinal": True}}
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/connect+json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        + _build_chunked_body(
            [
                _build_connect_envelope(partial),
                _build_connect_envelope(final),
                _build_connect_envelope({"complete": {}}),
            ]
        )
    )
    service._stream = _StreamConn(_MockSocket(response))  # type: ignore[arg-type]
    service._session_id = "session-final"
    pushed: list[Any] = []

    async def capture(frame: Any, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pushed.append(frame)

    service.push_frame = capture  # type: ignore[method-assign]
    await service._stream_reader_loop()

    finals = [frame for frame in pushed if isinstance(frame, TranscriptionFrame)]
    assert [frame.text for frame in finals] == ["same words"]
    assert sum(isinstance(frame, UserStartedSpeakingFrame) for frame in pushed) == 1
    assert sum(isinstance(frame, UserStoppedSpeakingFrame) for frame in pushed) == 1
    service._session_id = None
    await service._close_session()


@pytest.mark.asyncio
async def test_provider_final_and_resumed_speech_are_merged_into_one_turn() -> None:
    service = AntigravityLiveSTTService(
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    envelopes = [
        {"transcription": {"text": "My question is why", "isFinal": True}},
        {"transcription": {"text": "you called me", "isFinal": False}},
        {"transcription": {"text": "you called me today", "isFinal": True}},
        {"complete": {}},
    ]
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/connect+json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + _build_chunked_body([_build_connect_envelope(item) for item in envelopes])
    )
    service._stream = _StreamConn(_MockSocket(response))  # type: ignore[arg-type]
    service._session_id = "session-continuation"
    pushed: list[Any] = []

    async def capture(frame: Any, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pushed.append(frame)

    service.push_frame = capture  # type: ignore[method-assign]
    await service._stream_reader_loop()

    finals = [frame.text for frame in pushed if isinstance(frame, TranscriptionFrame)]
    assert finals == ["My question is why you called me today"]
    assert sum(isinstance(frame, UserStoppedSpeakingFrame) for frame in pushed) == 1
    service._session_id = None
    await service._close_session()


@pytest.mark.asyncio
async def test_short_natural_pause_does_not_finalize_the_turn() -> None:
    service = AntigravityLiveSTTService(
        silence_endpoint_ms=900,
        incomplete_endpoint_ms=1500,
        transcript_stability_ms=100,
        fallback_endpoint_ms=1800,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    now = time.monotonic()
    service._stage_transcription("My question is why", is_final=True)
    service._last_speech_at = now - 0.4
    service._last_transcript_update_at = now - 0.3
    pushed: list[Any] = []

    async def capture(frame: Any, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pushed.append(frame)

    service.push_frame = capture  # type: ignore[method-assign]
    service._watchdog_task = asyncio.create_task(service._silence_watchdog_loop())
    await asyncio.sleep(0.12)

    assert not any(isinstance(frame, TranscriptionFrame) for frame in pushed)
    await service._close_session()


@pytest.mark.asyncio
async def test_incomplete_question_gets_extended_endpoint_grace() -> None:
    service = AntigravityLiveSTTService(
        silence_endpoint_ms=200,
        incomplete_endpoint_ms=500,
        transcript_stability_ms=50,
        fallback_endpoint_ms=700,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    now = time.monotonic()
    service._stage_transcription("why", is_final=True)
    service._last_speech_at = now - 0.3
    service._last_transcript_update_at = now - 0.1
    pushed: list[Any] = []

    async def capture(frame: Any, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pushed.append(frame)

    service.push_frame = capture  # type: ignore[method-assign]
    service._watchdog_task = asyncio.create_task(service._silence_watchdog_loop())
    await asyncio.sleep(0.1)
    assert not any(isinstance(frame, TranscriptionFrame) for frame in pushed)

    service._last_speech_at = time.monotonic() - 0.6
    await asyncio.sleep(0.1)
    assert [frame.text for frame in pushed if isinstance(frame, TranscriptionFrame)] == ["why"]
    await service._close_session()


@pytest.mark.asyncio
async def test_late_revision_without_new_speech_is_not_a_second_turn() -> None:
    service = AntigravityLiveSTTService(
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    service._stage_transcription("first caller turn", is_final=True)
    expected_update_at = service._last_transcript_update_at

    async def capture(frame: Any, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        if isinstance(frame, TranscriptionFrame):
            service._stage_transcription("second caller turn", is_final=False)

    service.push_frame = capture  # type: ignore[method-assign]
    await service._commit_pending_transcript(
        source="test",
        expected_update_at=expected_update_at,
    )

    assert service._last_transcript == ""
    await service._close_session()


def test_speculative_endpointing_keeps_extra_grace_for_incomplete_speech() -> None:
    service = AntigravityLiveSTTService(
        speculative_pipeline_enabled=True,
        speculative_fast_endpoint_ms=450,
        speculative_ambiguous_endpoint_ms=700,
        speculative_incomplete_endpoint_ms=1100,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )

    service._stage_transcription("Can you help me?", is_final=True)
    assert service._required_silence() == pytest.approx(0.45)
    service._last_transcript = "I need help because"
    assert service._required_silence() == pytest.approx(1.1)


def test_dial_connector_gets_continuation_grace() -> None:
    service = AntigravityLiveSTTService(
        speculative_pipeline_enabled=True,
        speculative_incomplete_endpoint_ms=1100,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )

    service._stage_transcription("ديال", is_final=True)

    assert service._looks_incomplete("ديال")
    assert service._required_silence() == pytest.approx(1.1)


def test_non_final_speculation_cannot_become_an_authoritative_fast_turn() -> None:
    service = AntigravityLiveSTTService(
        speculative_pipeline_enabled=True,
        fallback_endpoint_ms=1800,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )

    service._stage_transcription(
        "les matchs de football Sport Sport Sport Matches Champions League",
        is_final=False,
    )

    assert service._required_silence() == pytest.approx(1.8)


def test_correct_english_final_replaces_cross_language_hypothesis() -> None:
    service = AntigravityLiveSTTService(
        speculative_pipeline_enabled=True,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    false_text = "les matchs de football Sport Matches Champions League"
    correct_text = "I watch football matches, Champions League, like this."

    service._stage_transcription(false_text, is_final=True)
    candidate = service._stage_transcription(correct_text, is_final=True)

    assert candidate == correct_text
    assert service._last_transcript == correct_text
    assert service._required_silence() == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_exact_cross_language_revision_produces_only_one_authoritative_turn() -> None:
    service = AntigravityLiveSTTService(
        speculative_pipeline_enabled=True,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    service._speech_epoch = 1
    false_text = "les matchs de football Sport Sport Sport Matches Champions League"
    correct_text = "I watch football matches, Champions League, like this."
    pushed: list[Any] = []

    async def capture(frame: Any, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pushed.append(frame)

    service.push_frame = capture  # type: ignore[method-assign]
    service._stage_transcription(false_text, is_final=False)
    assert service._required_silence() == pytest.approx(1.8)
    service._stage_transcription(correct_text, is_final=True)
    await service._commit_pending_transcript(source="test")

    finals = [frame.text for frame in pushed if isinstance(frame, TranscriptionFrame)]
    assert finals == [correct_text]
    assert service._stage_transcription(false_text, is_final=True) == ""


def test_new_acoustic_speech_epoch_allows_a_real_follow_up_turn() -> None:
    service = AntigravityLiveSTTService(
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    service._last_committed_text = "first caller turn"
    service._last_committed_at = time.monotonic()
    service._last_committed_speech_epoch = 1
    service._speech_epoch = 2

    assert service._stage_transcription("second caller turn", is_final=True) == (
        "second caller turn"
    )


def test_go_ahead_fragment_gets_continuation_grace() -> None:
    service = AntigravityLiveSTTService(
        speculative_pipeline_enabled=True,
        speculative_incomplete_endpoint_ms=1100,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )

    service._stage_transcription("Yes, it is a good time, please go", is_final=True)

    assert service._required_silence() == pytest.approx(1.1)


@pytest.mark.asyncio
async def test_speculation_signal_fires_only_after_stable_silence() -> None:
    service = AntigravityLiveSTTService(
        speculative_pipeline_enabled=True,
        speculative_prefetch_silence_ms=100,
        speculative_prefetch_stability_ms=80,
        speculative_fast_endpoint_ms=600,
        base_url="https://127.0.0.1:53857",
        csrf_token="test-csrf-token",
    )
    candidates: list[str] = []
    service.set_speculation_handlers(candidates.append, None)
    service._stage_transcription("Please check my order.", is_final=True)
    now = time.monotonic()
    service._last_speech_at = now - 0.2
    service._last_transcript_update_at = now - 0.15
    service._watchdog_task = asyncio.create_task(service._silence_watchdog_loop())

    await asyncio.sleep(0.08)

    assert candidates == ["Please check my order."]
    await service._close_session()
