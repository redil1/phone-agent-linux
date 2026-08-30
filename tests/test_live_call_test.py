"""Offline tests for race-free live-call orchestration."""

from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from phone_agent_gateway.mac_client.audio_probe import CaptureResult, ToneResult
from phone_agent_gateway.mac_client.gateway_client import CallState, CallStatus
from phone_agent_gateway.mac_client.live_call_test import LiveCallTestRunner


def status(state: CallState) -> CallStatus:
    return CallStatus("ok", state, 0, "")


class FakeClient:
    host = "127.0.0.1"

    def __init__(self, states: list[CallState]) -> None:
        self.states = deque(states)
        self.last_state = states[0]
        self.dialed = False
        self.hangups = 0

    def get_status(self) -> CallStatus:
        if self.states:
            self.last_state = self.states.popleft()
        return status(self.last_state)

    def dial(self, number: str) -> dict:
        self.dialed = True
        return {"status": "ok", "action": "dialing"}

    def get_audio_status(self) -> dict:
        return {
            "status": "ok",
            "rx_connected": True,
            "capture_source": "VOICE_DOWNLINK",
            "tx_connected": True,
            "injection_route": "telephony_tx_candidate_unverified",
        }

    def hangup(self) -> dict:
        self.hangups += 1
        self.last_state = CallState.IDLE
        return {"status": "ok", "action": "hung_up"}

    def flush_audio(self) -> dict:
        return {"status": "ok", "action": "audio_flushed", "generation": 2}


def successful_capture(host, port, seconds, output, *, stop_event, connected_event):
    connected_event.set()
    stop_event.wait(timeout=0.25)
    return CaptureResult(Path(output), 3200, 0.1)


def test_capture_attaches_in_same_flow_that_detects_active(tmp_path: Path) -> None:
    client = FakeClient([
        CallState.IDLE,
        CallState.IDLE,
        CallState.DIALING,
        CallState.ACTIVE,
        CallState.ACTIVE,
    ])
    observed = {}

    def capture(*args, **kwargs):
        observed["state_at_capture"] = client.last_state
        return successful_capture(*args, **kwargs)

    runner = LiveCallTestRunner(client, capture_function=capture, poll_interval=0.001)
    result = runner.run("0612345678", tmp_path / "call.wav", capture_seconds=0.01)

    assert observed["state_at_capture"] == CallState.ACTIVE
    assert result.capture.bytes_received == 3200
    assert result.audio_status_while_attached["capture_source"] == "VOICE_DOWNLINK"
    assert result.hangup_result == {"status": "ok", "action": "hung_up"}
    assert client.hangups == 1


def test_remote_disconnect_stops_capture_and_preserves_result(tmp_path: Path) -> None:
    client = FakeClient([
        CallState.IDLE,
        CallState.DIALING,
        CallState.ACTIVE,
        CallState.ACTIVE,
        CallState.DISCONNECTED,
    ])
    runner = LiveCallTestRunner(client, capture_function=successful_capture, poll_interval=0.001)

    result = runner.run("0612345678", tmp_path / "call.wav", capture_seconds=5.0)

    assert result.outcome == "remote_disconnect"
    assert result.capture.duration_seconds == 0.1
    assert client.hangups == 0


def test_capture_failure_still_hangs_up_live_call(tmp_path: Path) -> None:
    client = FakeClient([
        CallState.IDLE,
        CallState.DIALING,
        CallState.ACTIVE,
        CallState.ACTIVE,
    ])

    def failed_capture(host, port, seconds, output, *, stop_event, connected_event):
        connected_event.set()
        raise OSError("synthetic capture failure")

    runner = LiveCallTestRunner(client, capture_function=failed_capture, poll_interval=0.001)

    try:
        runner.run("0612345678", tmp_path / "call.wav", capture_seconds=1.0)
    except RuntimeError as exc:
        assert "synthetic capture failure" in str(exc)
    else:
        raise AssertionError("capture failure should propagate")

    assert client.hangups == 1


def test_tone_injection_runs_during_active_capture(tmp_path: Path) -> None:
    client = FakeClient([
        CallState.IDLE,
        CallState.DIALING,
        CallState.ACTIVE,
        CallState.ACTIVE,
    ])
    observed = {}

    def successful_tone(
        client_arg, host, port, seconds, frequency, amplitude, *, stop_event,
        connected_event, flush_on_complete
    ):
        observed["state_at_tone"] = client.last_state
        connected_event.set()
        return ToneResult(frequency, amplitude, 32000, seconds, {"status": "ok"})

    runner = LiveCallTestRunner(
        client,
        capture_function=successful_capture,
        tone_function=successful_tone,
        poll_interval=0.001,
    )
    result = runner.run(
        "0612345678",
        tmp_path / "call.wav",
        capture_seconds=0.6,
        tone_delay=0.0,
        tone_seconds=0.01,
    )

    assert observed["state_at_tone"] == CallState.ACTIVE
    assert result.injection is not None
    assert result.injection.bytes_sent == 32000
    assert result.audio_status_while_injecting["tx_connected"] is True


def test_barge_in_flushes_tone_after_two_loud_frames(tmp_path: Path) -> None:
    client = FakeClient([
        CallState.IDLE,
        CallState.DIALING,
        CallState.ACTIVE,
        CallState.ACTIVE,
    ])

    def capture_with_speech(host, port, seconds, output, *, stop_event, connected_event,
                            frame_callback):
        connected_event.set()
        # Give the control loop time to start and arm the tone connection.
        for _ in range(100):
            if stop_event.wait(timeout=0.002):
                break
            frame = (10000).to_bytes(2, "little", signed=True) * 320
            frame_callback(frame, time.monotonic())
        stop_event.wait(timeout=0.25)
        return CaptureResult(Path(output), 6400, 0.2)

    def interruptible_tone(
        client_arg, host, port, seconds, frequency, amplitude, *, stop_event,
        connected_event, flush_on_complete
    ):
        connected_event.set()
        stop_event.wait(timeout=0.5)
        return ToneResult(frequency, amplitude, 6400, 0.2, None)

    runner = LiveCallTestRunner(
        client,
        capture_function=capture_with_speech,
        tone_function=interruptible_tone,
        poll_interval=0.001,
    )
    result = runner.run(
        "0612345678",
        tmp_path / "call.wav",
        capture_seconds=0.7,
        tone_delay=0.0,
        tone_seconds=0.1,
        barge_in=True,
        post_barge_seconds=0.01,
    )

    assert result.barge_in is not None
    assert result.barge_in.flush_result["action"] == "audio_flushed"
    assert result.barge_in.consecutive_frames == 2
    assert result.barge_in.onset_to_flush_ack_ms >= 0
