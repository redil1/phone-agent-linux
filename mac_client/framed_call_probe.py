#!/usr/bin/env python3
"""Safety-gated real-call proof for authenticated PHAG v1 media and flush."""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import threading
import time
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path

from phone_agent_gateway.ai_bridge.media_protocol import MediaFrame
from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase

from .framed_link import load_link_key
from .gateway_client import CallState
from .protocol_client import AuthenticatedPhoneAgentClient, wait_for_state

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2


@dataclass(slots=True)
class ProbeResult:
    call_id: str
    link_epoch: str
    downlink_frames: int
    downlink_bytes: int
    peak_dbfs: float
    flush_generation: int
    flush_latency_ms: float | None
    stale_before: int
    stale_after: int
    stale_rejection_proven: bool
    wav_path: str


def dbfs(payload: bytes) -> float:
    samples = array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return -120.0
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    if mean_square <= 0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(mean_square) / 32768.0)


def tone_frame(frequency: float, amplitude: float, frame_index: int) -> bytes:
    samples = []
    base = frame_index * FRAME_SAMPLES
    for offset in range(FRAME_SAMPLES):
        value = amplitude * math.sin(2 * math.pi * frequency * (base + offset) / SAMPLE_RATE)
        samples.append(max(-32768, min(32767, round(value * 32767))))
    return struct.pack("<" + "h" * len(samples), *samples)


def run(args: argparse.Namespace) -> ProbeResult:
    if not args.confirm_call or not args.confirm_tone:
        raise RuntimeError("--confirm-call and --confirm-tone are both required")
    key = load_link_key(args.key_file)
    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    client = AuthenticatedPhoneAgentClient(session, key, device_id=args.device_id)

    captured = bytearray()
    capture_lock = threading.Lock()
    tone_active = threading.Event()
    caller_speech = threading.Event()
    consecutive = 0
    onset_ns = 0
    peak = -120.0

    def receive(frame: MediaFrame) -> None:
        nonlocal consecutive, onset_ns, peak
        level = dbfs(frame.payload)
        with capture_lock:
            captured.extend(frame.payload)
            peak = max(peak, level)
        if not tone_active.is_set():
            return
        if level >= args.barge_threshold_dbfs:
            consecutive += 1
            if consecutive == 1:
                onset_ns = time.monotonic_ns()
            if consecutive >= args.barge_frames:
                caller_speech.set()
        else:
            consecutive = 0
            onset_ns = 0

    client.link.on_audio_received(receive)
    flush_generation = session.generation_id
    flush_latency_ms: float | None = None
    stale_before = 0
    stale_after = 0
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    wav_path = output_dir / f"framed-call-{stamp}.wav"
    result_path = output_dir / f"framed-call-{stamp}.json"

    try:
        client.connect_control()
        response = client.dial(args.number)
        if response.get("status") != "ok":
            raise RuntimeError(f"dial failed: {response}")
        print("Dialing. Answer the call; speak while the test tone is audible.", flush=True)
        wait_for_state(client, {CallState.ACTIVE}, timeout=args.active_timeout)
        session.set_phase(SessionPhase.ACTIVE)
        client.connect_media()
        time.sleep(args.tone_delay)
        stale_before = int(client.get_audio_status()["audio"]["stale_uplink_frames"])

        tone_active.set()
        total_frames = round(args.tone_seconds * 1000 / FRAME_MS)
        last_sequence = -1
        for frame_index in range(total_frames):
            generation, sequence = session.next_output_identity()
            last_sequence = sequence
            payload = tone_frame(args.tone_frequency, args.tone_amplitude, frame_index)
            client.link.send_audio_chunk(payload, generation, sequence)
            session.account_output(generation, sequence, len(payload))
            if caller_speech.wait(FRAME_MS / 1000):
                break

        if caller_speech.is_set():
            advance = session.interrupt("caller_speech_probe")
            acknowledged = client.flush_audio(advance)
            flush_generation = int(acknowledged["generation"])
            if onset_ns:
                flush_latency_ms = (time.monotonic_ns() - onset_ns) / 1_000_000
            session.finish_interruption()

            stale_payload = tone_frame(args.tone_frequency, args.tone_amplitude, total_frames + 1)
            client.link.send_audio_chunk(
                stale_payload,
                advance.cancelled_generation,
                last_sequence + 10_000,
            )
            time.sleep(0.25)
        else:
            raise RuntimeError("caller speech was not detected while the tone was active")

        stale_after = int(client.get_audio_status()["audio"]["stale_uplink_frames"])
        with capture_lock:
            audio = bytes(captured)
            peak_level = peak
        with wave.open(str(wav_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(SAMPLE_RATE)
            output.writeframes(audio)

        result = ProbeResult(
            call_id=str(session.call_id),
            link_epoch=str(session.link_epoch),
            downlink_frames=len(audio) // FRAME_BYTES,
            downlink_bytes=len(audio),
            peak_dbfs=peak_level,
            flush_generation=flush_generation,
            flush_latency_ms=flush_latency_ms,
            stale_before=stale_before,
            stale_after=stale_after,
            stale_rejection_proven=stale_after > stale_before,
            wav_path=str(wav_path),
        )
        result_path.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
        print(json.dumps(asdict(result), indent=2), flush=True)
        return result
    finally:
        try:
            status = client.get_status()
            if status.state not in {CallState.IDLE, CallState.DISCONNECTED}:
                client.hangup()
        except Exception:
            pass
        client.close()
        phase = session.snapshot().phase
        if phase in {SessionPhase.CONNECTING, SessionPhase.ACTIVE}:
            session.set_phase(SessionPhase.ENDING)
            session.set_phase(SessionPhase.CLOSED)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("number")
    parser.add_argument("--confirm-call", action="store_true")
    parser.add_argument("--confirm-tone", action="store_true")
    parser.add_argument("--device-id")
    parser.add_argument(
        "--key-file",
        default=str(Path.home() / ".config" / "phone-agent" / "link.key"),
    )
    parser.add_argument("--output-dir", default="artifacts/framed-calls")
    parser.add_argument("--active-timeout", type=float, default=45.0)
    parser.add_argument("--tone-delay", type=float, default=1.5)
    parser.add_argument("--tone-seconds", type=float, default=8.0)
    parser.add_argument("--tone-frequency", type=float, default=1000.0)
    parser.add_argument("--tone-amplitude", type=float, default=0.05)
    parser.add_argument("--barge-threshold-dbfs", type=float, default=-42.0)
    parser.add_argument("--barge-frames", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    try:
        result = run(parse_args())
    except Exception as exc:
        print(f"Framed call probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    if not result.stale_rejection_proven:
        raise SystemExit("Android did not report rejecting the cancelled-generation frame")


if __name__ == "__main__":
    main()
