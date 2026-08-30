#!/usr/bin/env python3
"""Race-free live cellular call and downlink-capture test.

This command deliberately requires ``--confirm-dial``.  It owns the complete
test lifecycle so capture is attached in the same polling loop that observes
the transition to ACTIVE.  It always attempts to end a call that it placed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from array import array
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from phone_agent_gateway.mac_client.audio_probe import (
    CaptureResult,
    ToneResult,
    capture_wav,
    inject_tone,
)
from phone_agent_gateway.mac_client.gateway_client import CallState, PhoneAgentClient

LIVE_STATES = {
    CallState.NEW,
    CallState.DIALING,
    CallState.CONNECTING,
    CallState.ACTIVE,
    CallState.HOLDING,
}
TERMINAL_STATES = {CallState.IDLE, CallState.DISCONNECTED}


@dataclass(frozen=True)
class LiveCallTestResult:
    outcome: str
    capture: CaptureResult
    active_detected_at: float
    capture_connected_at: float
    attachment_latency_ms: float
    final_call_state: str
    audio_status_while_attached: dict[str, Any]
    injection: ToneResult | None
    audio_status_while_injecting: dict[str, Any]
    barge_in: BargeInResult | None
    hangup_result: dict[str, Any] | None

    def to_json(self) -> str:
        payload = asdict(self)
        payload["capture"]["output"] = str(self.capture.output)
        return json.dumps(payload, indent=2, sort_keys=True)


CaptureFunction = Callable[..., CaptureResult]
ToneFunction = Callable[..., ToneResult]


@dataclass(frozen=True)
class BargeInResult:
    speech_onset_monotonic: float
    flush_ack_monotonic: float
    frame_rms_dbfs: float
    threshold_dbfs: float
    consecutive_frames: int
    onset_to_flush_ack_ms: float
    flush_result: dict[str, Any]


class LiveCallTestRunner:
    """Coordinates call control and audio attachment without a process gap."""

    def __init__(
        self,
        client: PhoneAgentClient,
        *,
        capture_function: CaptureFunction = capture_wav,
        tone_function: ToneFunction = inject_tone,
        poll_interval: float = 0.05,
    ) -> None:
        self.client = client
        self.capture_function = capture_function
        self.tone_function = tone_function
        self.poll_interval = poll_interval

    def run(
        self,
        number: str,
        output: Path,
        *,
        capture_seconds: float = 30.0,
        answer_timeout: float = 60.0,
        rx_port: int = 8766,
        tone_delay: float | None = None,
        tone_seconds: float = 1.0,
        tone_frequency: float = 1000.0,
        tone_amplitude: float = 0.05,
        tx_port: int = 8767,
        barge_in: bool = False,
        barge_threshold_dbfs: float = -42.0,
        barge_consecutive_frames: int = 2,
        post_barge_seconds: float = 2.0,
    ) -> LiveCallTestResult:
        if capture_seconds <= 0:
            raise ValueError("capture_seconds must be positive")
        if answer_timeout <= 0:
            raise ValueError("answer_timeout must be positive")
        if tone_delay is not None:
            if tone_delay < 0:
                raise ValueError("tone_delay cannot be negative")
            if not 0 < tone_seconds <= 3.0:
                raise ValueError("tone_seconds must be between 0 and 3")
            if not 200.0 <= tone_frequency <= 3000.0:
                raise ValueError("tone_frequency must be between 200 and 3000 Hz")
            if not 0 < tone_amplitude <= 0.2:
                raise ValueError("tone_amplitude must be between 0 and 0.2")
            if tone_delay + tone_seconds + 0.5 >= capture_seconds:
                raise ValueError("capture duration must extend at least 0.5s beyond the tone")
        if barge_in:
            if tone_delay is None:
                raise ValueError("barge-in testing requires tone injection")
            if not -70.0 <= barge_threshold_dbfs <= -10.0:
                raise ValueError("barge_threshold_dbfs must be between -70 and -10")
            if not 1 <= barge_consecutive_frames <= 10:
                raise ValueError("barge_consecutive_frames must be between 1 and 10")
            if post_barge_seconds < 0:
                raise ValueError("post_barge_seconds cannot be negative")

        initial = self.client.get_status()
        if initial.state not in TERMINAL_STATES:
            raise RuntimeError(f"Refusing to dial while call state is {initial.state.value}")

        dial_result = self.client.dial(number)
        if dial_result.get("status") != "ok":
            raise RuntimeError(f"Dial command failed: {dial_result.get('message', dial_result)}")

        placed_call = True
        stop_capture = threading.Event()
        capture_connected = threading.Event()
        capture_box: dict[str, Any] = {}
        capture_thread: threading.Thread | None = None
        stop_tone = threading.Event()
        tone_connected = threading.Event()
        tone_box: dict[str, Any] = {}
        barge_box: dict[str, Any] = {}
        tone_thread: threading.Thread | None = None
        hangup_result: dict[str, Any] | None = None
        active_at = 0.0
        connected_at = 0.0
        final_state = CallState.UNKNOWN
        attached_audio_status: dict[str, Any] = {}
        injecting_audio_status: dict[str, Any] = {}
        outcome = "unknown"
        capture_result: CaptureResult | None = None
        tone_result: ToneResult | None = None
        barge_result: BargeInResult | None = None

        try:
            active_at = self._wait_for_active(answer_timeout)

            above_threshold_frames = 0
            possible_onset = 0.0

            def on_downlink_frame(frame: bytes, received_at: float) -> None:
                nonlocal above_threshold_frames, possible_onset
                if not barge_in or not tone_connected.is_set() or stop_tone.is_set():
                    return
                samples = array("h")
                samples.frombytes(frame)
                if not samples:
                    return
                rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
                rms_dbfs = 20.0 * math.log10(rms / 32768.0) if rms else -120.0
                if rms_dbfs >= barge_threshold_dbfs:
                    if above_threshold_frames == 0:
                        possible_onset = received_at
                    above_threshold_frames += 1
                else:
                    above_threshold_frames = 0
                    possible_onset = 0.0
                    return
                if above_threshold_frames < barge_consecutive_frames or "result" in barge_box:
                    return

                # Stop the producer before advancing Android's generation so a
                # racing stale frame is discarded by the phone-side flush.
                stop_tone.set()
                flush_result = self.client.flush_audio()
                acknowledged_at = time.monotonic()
                barge_box["result"] = BargeInResult(
                    speech_onset_monotonic=possible_onset,
                    flush_ack_monotonic=acknowledged_at,
                    frame_rms_dbfs=rms_dbfs,
                    threshold_dbfs=barge_threshold_dbfs,
                    consecutive_frames=above_threshold_frames,
                    onset_to_flush_ack_ms=(acknowledged_at - possible_onset) * 1000.0,
                    flush_result=flush_result,
                )

            def capture_target() -> None:
                try:
                    capture_args = {
                        "stop_event": stop_capture,
                        "connected_event": capture_connected,
                    }
                    if barge_in:
                        capture_args["frame_callback"] = on_downlink_frame
                    capture_box["result"] = self.capture_function(
                        self.client.host, rx_port, capture_seconds, output, **capture_args
                    )
                except BaseException as exc:
                    capture_box["error"] = exc

            # Start in the same control flow that observed ACTIVE; there is no
            # second command/process boundary in which the call can be lost.
            capture_thread = threading.Thread(
                target=capture_target,
                name="live-call-downlink-capture",
                daemon=True,
            )
            capture_thread.start()

            attach_deadline = time.monotonic() + 3.0
            while not capture_connected.is_set() and time.monotonic() < attach_deadline:
                error = capture_box.get("error")
                if error is not None:
                    raise RuntimeError(f"Capture attachment failed: {error}") from error
                status = self.client.get_status()
                final_state = status.state
                if status.state in TERMINAL_STATES:
                    raise RuntimeError("Call ended before the downlink socket attached")
                time.sleep(self.poll_interval)
            if not capture_connected.is_set():
                raise TimeoutError("Downlink socket did not attach within 3 seconds")
            connected_at = time.monotonic()

            deadline = active_at + capture_seconds
            while time.monotonic() < deadline:
                if "error" in capture_box:
                    error = capture_box["error"]
                    raise RuntimeError(f"Capture failed: {error}") from error
                status = self.client.get_status()
                final_state = status.state
                if not attached_audio_status:
                    candidate = self.client.get_audio_status()
                    if candidate.get("rx_connected"):
                        attached_audio_status = candidate
                if tone_delay is not None and tone_thread is None \
                        and time.monotonic() >= active_at + tone_delay:
                    def tone_target() -> None:
                        try:
                            tone_box["result"] = self.tone_function(
                                self.client,
                                self.client.host,
                                tx_port,
                                tone_seconds,
                                tone_frequency,
                                tone_amplitude,
                                stop_event=stop_tone,
                                connected_event=tone_connected,
                                flush_on_complete=not barge_in,
                            )
                        except BaseException as exc:
                            tone_box["error"] = exc

                    tone_thread = threading.Thread(
                        target=tone_target,
                        name="live-call-uplink-tone",
                        daemon=True,
                    )
                    tone_thread.start()
                if "error" in tone_box:
                    error = tone_box["error"]
                    raise RuntimeError(f"Tone injection failed: {error}") from error
                if tone_connected.is_set() and not injecting_audio_status:
                    candidate = self.client.get_audio_status()
                    if candidate.get("tx_connected"):
                        injecting_audio_status = candidate
                if "result" in barge_box and barge_result is None:
                    barge_result = barge_box["result"]
                    deadline = min(deadline, barge_result.flush_ack_monotonic + post_barge_seconds)
                if status.state in TERMINAL_STATES:
                    outcome = "remote_disconnect"
                    break
                time.sleep(self.poll_interval)
            else:
                outcome = "capture_duration_reached"

            stop_capture.set()
            capture_thread.join(timeout=3.0)
            if capture_thread.is_alive():
                raise TimeoutError("Capture did not stop within 3 seconds")
            if "error" in capture_box:
                error = capture_box["error"]
                raise RuntimeError(f"Capture failed: {error}") from error
            capture_result = capture_box.get("result")
            if not isinstance(capture_result, CaptureResult):
                raise RuntimeError("Capture finished without a result")
            if tone_thread is not None:
                tone_thread.join(timeout=tone_seconds + 2.0)
                if tone_thread.is_alive():
                    raise TimeoutError("Tone injection did not stop in time")
                if "error" in tone_box:
                    error = tone_box["error"]
                    raise RuntimeError(f"Tone injection failed: {error}") from error
                candidate = tone_box.get("result")
                if isinstance(candidate, ToneResult):
                    tone_result = candidate
        finally:
            stop_capture.set()
            stop_tone.set()
            if capture_thread is not None and capture_thread.is_alive():
                capture_thread.join(timeout=1.0)
            if tone_thread is not None and tone_thread.is_alive():
                tone_thread.join(timeout=1.0)
            if placed_call:
                try:
                    current = self.client.get_status()
                    final_state = current.state
                    if current.state in LIVE_STATES:
                        hangup_result = self.client.hangup()
                        cleanup_deadline = time.monotonic() + 3.0
                        while time.monotonic() < cleanup_deadline:
                            current = self.client.get_status()
                            final_state = current.state
                            if current.state in TERMINAL_STATES:
                                break
                            time.sleep(self.poll_interval)
                except Exception:
                    # Preserve the original failure. CLI reports cleanup status
                    # with a final state check after this method returns/fails.
                    pass

        if capture_result is None:
            raise RuntimeError("Capture finished without a result")
        return LiveCallTestResult(
            outcome=outcome,
            capture=capture_result,
            active_detected_at=active_at,
            capture_connected_at=connected_at,
            attachment_latency_ms=(connected_at - active_at) * 1000.0,
            final_call_state=final_state.value,
            audio_status_while_attached=attached_audio_status,
            injection=tone_result,
            audio_status_while_injecting=injecting_audio_status,
            barge_in=barge_result,
            hangup_result=hangup_result,
        )

    def _wait_for_active(self, timeout: float) -> float:
        deadline = time.monotonic() + timeout
        saw_progress = False
        while time.monotonic() < deadline:
            status = self.client.get_status()
            if status.state == CallState.ACTIVE:
                return time.monotonic()
            if status.state in LIVE_STATES:
                saw_progress = True
            elif saw_progress and status.state in TERMINAL_STATES:
                raise RuntimeError("Call ended before becoming ACTIVE")
            time.sleep(self.poll_interval)
        raise TimeoutError(f"Call did not become ACTIVE within {timeout:.1f} seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--number", required=True, help="consenting test participant")
    parser.add_argument("--output", type=Path, required=True, help="WAV destination")
    parser.add_argument("--seconds", type=float, default=30.0, help="maximum capture duration")
    parser.add_argument("--answer-timeout", type=float, default=60.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=8765)
    parser.add_argument("--rx-port", type=int, default=8766)
    parser.add_argument("--tx-port", type=int, default=8767)
    parser.add_argument("--inject-tone", action="store_true", help="inject a short test tone")
    parser.add_argument("--tone-delay", type=float, default=4.0)
    parser.add_argument("--tone-seconds", type=float, default=1.0)
    parser.add_argument("--tone-frequency", type=float, default=1000.0)
    parser.add_argument("--tone-amplitude", type=float, default=0.05)
    parser.add_argument(
        "--barge-in",
        action="store_true",
        help="flush tone when caller speech is detected",
    )
    parser.add_argument("--barge-threshold-dbfs", type=float, default=-42.0)
    parser.add_argument("--barge-frames", type=int, default=2)
    parser.add_argument("--post-barge-seconds", type=float, default=2.0)
    parser.add_argument(
        "--confirm-tone",
        action="store_true",
        help="required confirmation that the recipient expects the diagnostic tone",
    )
    parser.add_argument(
        "--confirm-dial",
        action="store_true",
        help="required confirmation that the recipient expects this live call and recording",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_dial:
        raise RuntimeError("Refusing to dial without --confirm-dial")
    if args.inject_tone and not args.confirm_tone:
        raise RuntimeError("Refusing tone injection without --confirm-tone")
    if args.barge_in and not args.inject_tone:
        raise RuntimeError("Barge-in testing requires --inject-tone")

    client = PhoneAgentClient(host=args.host, port=args.control_port)
    try:
        runner = LiveCallTestRunner(client)
        result = runner.run(
            args.number,
            args.output.resolve(),
            capture_seconds=args.seconds,
            answer_timeout=args.answer_timeout,
            rx_port=args.rx_port,
            tone_delay=args.tone_delay if args.inject_tone else None,
            tone_seconds=args.tone_seconds,
            tone_frequency=args.tone_frequency,
            tone_amplitude=args.tone_amplitude,
            tx_port=args.tx_port,
            barge_in=args.barge_in,
            barge_threshold_dbfs=args.barge_threshold_dbfs,
            barge_consecutive_frames=args.barge_frames,
            post_barge_seconds=args.post_barge_seconds,
        )
        print(result.to_json())
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Live call test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
