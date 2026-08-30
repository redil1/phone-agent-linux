from __future__ import annotations

import json
import os
import time
import wave
from pathlib import Path

import pytest
from phone_agent_gateway.ai_bridge.call_recording import (
    CallRecordingSession,
    RecordingConfig,
    RecordingError,
    enforce_recording_retention,
)
from phone_agent_gateway.ai_bridge.pipecat_transport import PhoneAgentTransport


def _config(root: Path, *, enabled: bool = True, consent: bool = True) -> RecordingConfig:
    return RecordingConfig(
        enabled=enabled,
        consent_granted=consent,
        root=root,
        retention_days=30,
        queue_frames=100,
    )


def test_recording_requires_enablement_and_consent(tmp_path: Path) -> None:
    assert (
        CallRecordingSession.create_if_authorized(
            call_id="one",
            caller_id="+212600454425",
            channel="gsm",
            config=_config(tmp_path, enabled=False),
        )
        is None
    )
    assert (
        CallRecordingSession.create_if_authorized(
            call_id="one",
            caller_id="+212600454425",
            channel="gsm",
            config=_config(tmp_path, consent=False),
        )
        is None
    )


def test_recording_writes_aligned_private_tracks_and_manifest(tmp_path: Path) -> None:
    recorder = CallRecordingSession(
        call_id="call-1",
        caller_id="+212600454425",
        channel="gsm",
        sample_rate=16_000,
        config=_config(tmp_path),
    )
    pcm = (1000).to_bytes(2, "little", signed=True) * 320
    recorder.record_remote(pcm)
    recorder.record_agent(pcm)
    result = recorder.finalize(outcome="completed")
    assert result.complete
    assert result.dropped_frames == 0
    manifest = json.loads(result.manifest.read_text())
    assert manifest["consent"]["granted"] is True
    assert manifest["subject"].startswith("sha256:")
    assert "+212" not in result.manifest.read_text()
    assert set(manifest["files"]) == {"remote.wav", "agent.wav", "conversation.wav"}
    for name in manifest["files"]:
        path = result.directory / name
        assert os.stat(path).st_mode & 0o777 == 0o600
        with wave.open(str(path), "rb") as recording:
            assert recording.getframerate() == 16_000
            assert recording.getnchannels() == 1
            assert recording.getnframes() >= 320
    assert os.stat(result.directory).st_mode & 0o777 == 0o700


def test_generic_transport_observers_capture_raw_input_and_delivered_output(
    tmp_path: Path,
) -> None:
    recorder = CallRecordingSession(
        call_id="call-transport",
        caller_id="anonymous",
        channel="provider-independent",
        config=_config(tmp_path),
    )
    transport = PhoneAgentTransport()

    class FakeInput:
        def accept_audio_bytes(self, pcm: bytes, *, enqueue: bool) -> None:
            assert pcm and enqueue

    transport._input = FakeInput()  # type: ignore[assignment]
    transport.add_audio_listener(recorder.record_remote)
    transport.add_output_audio_listener(recorder.record_agent)
    pcm = b"\0\0" * 320
    transport.feed_audio_bytes(pcm)
    transport.notify_output_audio(pcm)
    transport.remove_audio_listener(recorder.record_remote)
    transport.remove_output_audio_listener(recorder.record_agent)
    result = recorder.finalize(outcome="transport_test")
    assert result.complete


def test_malformed_pcm_marks_recording_incomplete(tmp_path: Path) -> None:
    recorder = CallRecordingSession(
        call_id="call-2",
        caller_id="anonymous",
        channel="whatsapp",
        config=_config(tmp_path),
    )
    recorder.record_remote(b"bad")
    recorder.record_agent(b"\0\0" * 320)
    result = recorder.finalize(outcome="completed")
    assert not result.complete
    assert result.dropped_frames == 1


def test_recording_root_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "recordings"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(RecordingError):
        CallRecordingSession(
            call_id="call-3",
            caller_id="anonymous",
            channel="gsm",
            config=_config(link),
        )


def test_retention_removes_only_expired_real_directories(tmp_path: Path) -> None:
    old = tmp_path / "old"
    recent = tmp_path / "recent"
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    old.mkdir()
    recent.mkdir()
    outside.mkdir(exist_ok=True)
    link = tmp_path / "linked"
    link.symlink_to(outside, target_is_directory=True)
    old_time = time.time() - 40 * 86_400
    os.utime(old, (old_time, old_time))
    removed = enforce_recording_retention(_config(tmp_path), now=time.time())
    assert removed == 1
    assert not old.exists()
    assert recent.exists()
    assert link.is_symlink()
    assert outside.exists()
    outside.rmdir()
