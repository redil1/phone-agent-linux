#!/usr/bin/env python3
"""Controlled real-call audio feasibility probe.

This tool never dials or answers. Capture and injection require an already
ACTIVE call. Tone injection additionally requires --confirm-live-call.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import struct
import sys
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from phone_agent_gateway.mac_client.gateway_client import CallState, PhoneAgentClient

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
CHUNK_SAMPLES = 320
CHUNK_BYTES = CHUNK_SAMPLES * SAMPLE_WIDTH


@dataclass(frozen=True)
class CaptureResult:
    """Facts about PCM actually received from the phone."""

    output: Path
    bytes_received: int
    duration_seconds: float


@dataclass(frozen=True)
class ToneResult:
    """Facts about diagnostic PCM accepted by the Android uplink socket."""

    frequency_hz: float
    amplitude: float
    bytes_sent: int
    duration_seconds: float
    flush_result: dict | None


def require_active_call(client: PhoneAgentClient) -> None:
    status = client.get_status()
    if status.state != CallState.ACTIVE:
        raise RuntimeError(
            f"Audio probe requires ACTIVE call; current state is {status.state.value}"
        )


def capture_wav(
    host: str,
    port: int,
    seconds: float,
    output: Path,
    *,
    stop_event: threading.Event | None = None,
    connected_event: threading.Event | None = None,
    frame_callback: Callable[[bytes, float], None] | None = None,
) -> CaptureResult:
    """Capture downlink until the deadline or an orchestrator requests stop.

    ``connected_event`` is raised as soon as the media socket is attached.  A
    short socket timeout lets a call-state monitor stop capture promptly when
    Telecom reports that the call ended.
    """
    deadline = time.monotonic() + seconds
    cancellation = stop_event or threading.Event()
    received = 0
    callback_buffer = bytearray()
    output.parent.mkdir(parents=True, exist_ok=True)
    with socket.create_connection((host, port), timeout=5) as stream:
        stream.settimeout(0.25)
        if connected_event is not None:
            connected_event.set()
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            while time.monotonic() < deadline and not cancellation.is_set():
                try:
                    chunk = stream.recv(CHUNK_BYTES)
                except TimeoutError:
                    continue
                if not chunk:
                    raise ConnectionError("Phone closed the downlink stream")
                wav.writeframesraw(chunk)
                received += len(chunk)
                if frame_callback is not None:
                    callback_buffer.extend(chunk)
                    while len(callback_buffer) >= CHUNK_BYTES:
                        frame = bytes(callback_buffer[:CHUNK_BYTES])
                        del callback_buffer[:CHUNK_BYTES]
                        frame_callback(frame, time.monotonic())
    if received == 0:
        raise RuntimeError("Downlink socket produced no audio bytes")
    duration = received / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
    result = CaptureResult(output=output, bytes_received=received, duration_seconds=duration)
    print(f"Captured {duration:.2f}s ({received} bytes) to {output}")
    return result


def tone_chunk(frequency: float, amplitude: float, start_sample: int) -> bytes:
    samples = []
    scale = max(0.0, min(amplitude, 0.8)) * 32767
    for offset in range(CHUNK_SAMPLES):
        phase = 2.0 * math.pi * frequency * (start_sample + offset) / SAMPLE_RATE
        samples.append(int(scale * math.sin(phase)))
    return struct.pack("<" + "h" * len(samples), *samples)


def inject_tone(
    client: PhoneAgentClient,
    host: str,
    port: int,
    seconds: float,
    frequency: float,
    amplitude: float,
    *,
    stop_event: threading.Event | None = None,
    connected_event: threading.Event | None = None,
    flush_on_complete: bool = True,
) -> ToneResult:
    cancellation = stop_event or threading.Event()
    total_chunks = max(1, int(seconds * SAMPLE_RATE / CHUNK_SAMPLES))
    next_send = time.monotonic()
    sent_chunks = 0
    with socket.create_connection((host, port), timeout=5) as stream:
        stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if connected_event is not None:
            connected_event.set()
        for index in range(total_chunks):
            if cancellation.is_set():
                break
            stream.sendall(tone_chunk(frequency, amplitude, index * CHUNK_SAMPLES))
            sent_chunks += 1
            next_send += CHUNK_SAMPLES / SAMPLE_RATE
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        # Let the small Android AudioTrack queue render its final frames before
        # EOF causes the phone to release the track.
        if sent_chunks and not cancellation.is_set():
            time.sleep(0.2)
    flush_result = client.flush_audio() if flush_on_complete else None
    duration = sent_chunks * CHUNK_SAMPLES / SAMPLE_RATE
    result = ToneResult(
        frequency_hz=frequency,
        amplitude=amplitude,
        bytes_sent=sent_chunks * CHUNK_BYTES,
        duration_seconds=duration,
        flush_result=flush_result,
    )
    print(f"Injected {frequency:.1f} Hz for {duration:.2f}s; final flush={flush_result}")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=8765)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="show readiness without opening media sockets")

    capture = subparsers.add_parser("capture", help="capture active-call downlink into WAV")
    capture.add_argument("--seconds", type=float, default=10.0)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--rx-port", type=int, default=8766)

    inject = subparsers.add_parser("inject-tone", help="inject a known tone into an active call")
    inject.add_argument("--seconds", type=float, default=2.0)
    inject.add_argument("--frequency", type=float, default=1000.0)
    inject.add_argument("--amplitude", type=float, default=0.12)
    inject.add_argument("--tx-port", type=int, default=8767)
    inject.add_argument(
        "--confirm-live-call",
        action="store_true",
        help="required acknowledgement that a remote participant expects the test tone",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = PhoneAgentClient(host=args.host, port=args.control_port)
    try:
        if args.command == "status":
            print(json.dumps(client.get_health(), indent=2, sort_keys=True))
            return 0
        require_active_call(client)
        if args.command == "capture":
            capture_wav(args.host, args.rx_port, args.seconds, args.output.resolve())
            return 0
        if args.command == "inject-tone":
            if not args.confirm_live_call:
                raise RuntimeError("Refusing live tone injection without --confirm-live-call")
            inject_tone(client, args.host, args.tx_port, args.seconds,
                        args.frequency, args.amplitude)
            return 0
        raise RuntimeError(f"Unknown command: {args.command}")
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Audio probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
