#!/usr/bin/env python3
"""Content-free offline preflight for the selected STT -> LLM -> TTS cascade."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from phone_agent_gateway.ai_bridge.media_protocol import (
    FrameDirection,
    FrameKind,
    MediaFrame,
)
from phone_agent_gateway.ai_bridge.pipecat_transport import (
    PhoneAgentTransport,
    PhoneAgentTransportParams,
)
from phone_agent_gateway.ai_bridge.production_pipeline import (
    ProductionCallPipeline,
    create_provider_services,
)
from phone_agent_gateway.ai_bridge.runtime_config import RuntimeConfig
from phone_agent_gateway.ai_bridge.session import SessionPhase

SAMPLE_RATE = 16_000
FRAME_BYTES = 640


@dataclass(slots=True)
class ProviderPreflightResult:
    stt_provider: str
    stt_model: str
    llm_provider: str
    llm_model: str
    tts_provider: str
    tts_model: str
    provider_load_ms: float
    greeting_first_audio_ms: float
    greeting_frames: int
    greeting_bytes: int
    loopback_response_first_audio_ms: float
    total_output_frames: int
    exact_20ms_output: bool
    context_role_counts: dict[str, int]
    telemetry_events: int


class AudioCapture:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.frames: list[bytes] = []
        self.timestamps: list[float] = []
        self.segments_ended = 0

    def send(self, payload: bytes, _generation: int, _sequence: int) -> None:
        with self._lock:
            self.frames.append(payload)
            self.timestamps.append(time.monotonic())

    def end_segment(self, _generation: int, _sequence: int) -> None:
        """Record the transport's authoritative end-of-speech marker.

        The output transport deliberately does not pace from Python, so a whole
        utterance arrives in a burst far shorter than its playout duration. An
        idle gap therefore says nothing about whether speech finished, and the
        end marker is the only sound signal that it did.
        """

        with self._lock:
            self.segments_ended += 1

    def snapshot(self) -> tuple[list[bytes], list[float]]:
        with self._lock:
            return list(self.frames), list(self.timestamps)

    def ended(self) -> int:
        with self._lock:
            return self.segments_ended


async def wait_for_output(
    capture: AudioCapture,
    minimum: int,
    timeout_seconds: float,
) -> float:
    started = time.monotonic()
    deadline = started + timeout_seconds
    while time.monotonic() < deadline:
        frames, timestamps = capture.snapshot()
        if len(frames) >= minimum:
            return timestamps[minimum - 1]
        await asyncio.sleep(0.02)
    raise TimeoutError(f"provider pipeline did not produce output frame {minimum}")


async def wait_for_segment_end(
    capture: AudioCapture,
    segments: int,
    *,
    timeout_seconds: float,
) -> tuple[list[bytes], list[float]]:
    """Wait for the transport to mark `segments` complete utterances.

    Replaces an idle-gap heuristic that truncated an utterance whenever
    synthesis stalled longer than the gap, which reported a partial greeting as
    a whole one and then fed that fragment back as the caller turn.
    """

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if capture.ended() >= segments:
            return capture.snapshot()
        await asyncio.sleep(0.02)
    raise TimeoutError(f"provider output never completed utterance {segments}")


async def wait_for_assistant_turns(
    pipeline: ProductionCallPipeline,
    turns: int,
    *,
    timeout_seconds: float,
) -> None:
    """Wait for the reply to be recorded, not merely started.

    The assistant aggregator commits a turn after playback, so asserting on the
    context the instant audio appears raced the pipeline and failed a cascade
    that was working.
    """

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if role_counts(pipeline).get("assistant", 0) >= turns:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("full local cascade did not complete its second LLM turn")


async def feed_realtime(
    transport: PhoneAgentTransport,
    payloads: list[bytes],
    *,
    first_sequence: int,
) -> tuple[int, float]:
    """Feed PCM at true 20 ms wall-clock pace and return the end-of-speech time.

    Sleeping a flat 20 ms per frame accumulates scheduler drift, which stretched
    a ten-second utterance well past real time and made the measured reply
    latency mostly harness overhead.
    """

    sequence = first_sequence
    started = time.monotonic()
    for index, payload in enumerate(payloads):
        transport.feed_phone_frame(
            MediaFrame(
                kind=FrameKind.AUDIO,
                direction=FrameDirection.PHONE_TO_MAC,
                call_id=transport.session.call_id,
                generation_id=transport.session.generation_id,
                sequence=sequence,
                monotonic_ns=time.monotonic_ns(),
                payload=payload,
                sample_rate=SAMPLE_RATE,
                channels=1,
                sample_width=2,
            )
        )
        sequence += 1
        remaining = started + (index + 1) * 0.02 - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
    return sequence, time.monotonic()


def role_counts(pipeline: ProductionCallPipeline) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for message in pipeline.context.messages:
        if isinstance(message, dict):
            counts[str(message.get("role", "unknown"))] += 1
    return dict(sorted(counts.items()))


async def run(args: argparse.Namespace) -> ProviderPreflightResult:
    config = RuntimeConfig.from_env(require_provider_credentials=True)
    loaded_at = time.monotonic()
    services = await asyncio.to_thread(
        create_provider_services,
        config.providers,
        config.sample_rate,
    )
    provider_load_ms = (time.monotonic() - loaded_at) * 1000

    transport = PhoneAgentTransport(
        PhoneAgentTransportParams(
            audio_in_sample_rate=config.sample_rate,
            audio_out_sample_rate=config.sample_rate,
            frame_ms=config.frame_ms,
            input_queue_frames=config.input_queue_frames,
        )
    )
    transport.session.set_phase(SessionPhase.CONNECTING)
    transport.session.set_phase(SessionPhase.ACTIVE)
    capture = AudioCapture()
    transport.set_tx_handler(capture.send)
    transport.set_audio_end_handler(capture.end_segment)
    transport.set_flush_handler(lambda _advance: {"status": "ok"})
    pipeline = ProductionCallPipeline(transport, config, services=services)

    try:
        await pipeline.start(timeout_secs=args.timeout)
        greeting_started = time.monotonic()
        await pipeline.greet()
        greeting_first_at = await wait_for_output(capture, 1, args.timeout)
        greeting_frames, _ = await wait_for_segment_end(
            capture,
            1,
            timeout_seconds=args.timeout,
        )
        greeting_count = len(greeting_frames)
        if not all(len(frame) == FRAME_BYTES for frame in greeting_frames):
            raise RuntimeError("provider output was not exact 20 ms PCM16/16k/mono")

        # Feed the synthesized greeting back as a caller utterance. This is a
        # deterministic, content-unlogged end-to-end STT -> LLM -> TTS test.
        sequence, speech_ended_at = await feed_realtime(
            transport, greeting_frames, first_sequence=0
        )
        # Trailing silence so the recognizer can endpoint the turn normally.
        sequence, _ = await feed_realtime(
            transport, [b"\x00" * FRAME_BYTES] * 100, first_sequence=sequence
        )

        # Latency is measured from the moment the caller stopped speaking, not
        # from the start of playback: the earlier form charged the whole
        # utterance to the agent and reported seconds for a one-second reply.
        second_first_at = await wait_for_output(capture, greeting_count + 1, args.timeout)
        await wait_for_assistant_turns(pipeline, 2, timeout_seconds=args.timeout)
        all_frames, _ = await wait_for_segment_end(
            capture,
            2,
            timeout_seconds=args.timeout,
        )
        roles = role_counts(pipeline)

        return ProviderPreflightResult(
            stt_provider=config.providers.stt_provider,
            stt_model=config.providers.stt_model,
            llm_provider=config.providers.llm_provider,
            llm_model=config.providers.llm_model,
            tts_provider=config.providers.tts_provider,
            tts_model=config.providers.tts_model,
            provider_load_ms=provider_load_ms,
            greeting_first_audio_ms=(greeting_first_at - greeting_started) * 1000,
            greeting_frames=greeting_count,
            greeting_bytes=sum(len(frame) for frame in greeting_frames),
            loopback_response_first_audio_ms=(second_first_at - speech_ended_at) * 1000,
            total_output_frames=len(all_frames),
            exact_20ms_output=all(len(frame) == FRAME_BYTES for frame in all_frames),
            context_role_counts=roles,
            telemetry_events=len(pipeline.telemetry.snapshot()),
        )
    finally:
        await pipeline.stop(timeout_secs=10.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--output",
        default="artifacts/provider-preflight/local-cascade.json",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        print(f"Provider preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(result), indent=2, sort_keys=True) + "\n")
    print(json.dumps(asdict(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
