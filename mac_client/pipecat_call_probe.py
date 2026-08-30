#!/usr/bin/env python3
"""Real cellular Work Package B proof through a Pipecat PipelineWorker."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionWorkerFrame,
    OutputAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from phone_agent_gateway.ai_bridge.pipecat_transport import (
    PhoneAgentTransport,
    PhoneAgentTransportParams,
)
from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase, TurnPhase

from .framed_call_probe import FRAME_MS, SAMPLE_RATE, dbfs, tone_frame
from .framed_link import load_link_key
from .gateway_client import CallState
from .protocol_client import AuthenticatedPhoneAgentClient, wait_for_state


@dataclass(slots=True)
class PipecatProbeResult:
    call_id: str
    link_epoch: str
    pipeline_started: bool
    pipecat_input_frames: int
    pipecat_output_frames: int
    downlink_bytes: int
    peak_dbfs: float
    flush_generation: int
    onset_to_flush_ack_ms: float | None
    stale_before: int
    stale_after: int
    stale_rejection_proven: bool
    wav_path: str


class CallerAudioSink(FrameProcessor):
    """Measure caller audio and prevent it from looping back into the uplink."""

    def __init__(self, *, threshold_dbfs: float, threshold_frames: int) -> None:
        super().__init__()
        self.threshold_dbfs = threshold_dbfs
        self.threshold_frames = threshold_frames
        self.audio = bytearray()
        self.input_frames = 0
        self.peak_dbfs = -120.0
        self.tone_active = False
        self.caller_speech = asyncio.Event()
        self.onset_ns = 0
        self._consecutive = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.audio.extend(frame.audio)
            self.input_frames += 1
            level = dbfs(frame.audio)
            self.peak_dbfs = max(self.peak_dbfs, level)
            if self.tone_active:
                if level >= self.threshold_dbfs:
                    self._consecutive += 1
                    if self._consecutive == 1:
                        self.onset_ns = time.monotonic_ns()
                    if self._consecutive >= self.threshold_frames:
                        self.caller_speech.set()
                else:
                    self._consecutive = 0
                    self.onset_ns = 0
            return
        await self.push_frame(frame, direction)


async def wait_for_flush(session: CallSessionState, previous_generation: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        snapshot = session.snapshot()
        if (
            snapshot.generation_id > previous_generation
            and snapshot.turn_phase is TurnPhase.LISTENING
        ):
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("Pipecat interruption did not finish the phone flush")


def prepare_output_dir(value: str) -> Path:
    output = Path(value).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    return output


def write_wav(path: Path, audio: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(audio)


async def run(args: argparse.Namespace) -> PipecatProbeResult:
    if not args.confirm_call or not args.confirm_tone:
        raise RuntimeError("--confirm-call and --confirm-tone are both required")

    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    client = AuthenticatedPhoneAgentClient(
        session,
        load_link_key(args.key_file),
        device_id=args.device_id,
    )
    transport = PhoneAgentTransport(
        PhoneAgentTransportParams(
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
            frame_ms=FRAME_MS,
        ),
        session=session,
    )
    client.link.on_audio_received(transport.feed_phone_frame)
    transport.set_tx_handler(client.link.send_audio_chunk)
    transport.set_flush_handler(client.flush_audio)

    sink = CallerAudioSink(
        threshold_dbfs=args.barge_threshold_dbfs,
        threshold_frames=args.barge_frames,
    )
    worker = PipelineWorker(
        Pipeline([transport.input(), sink, transport.output()]),
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
            enable_metrics=True,
        ),
        enable_rtvi=False,
    )
    started = asyncio.Event()

    @worker.event_handler("on_pipeline_started")
    async def on_pipeline_started(_worker, _frame) -> None:
        started.set()

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    runner_task: asyncio.Task | None = None
    stale_before = 0
    stale_after = 0
    output_frames = 0
    flush_generation = session.generation_id
    onset_to_ack: float | None = None
    output_dir = await asyncio.to_thread(prepare_output_dir, args.output_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    wav_path = output_dir / f"pipecat-call-{stamp}.wav"
    json_path = output_dir / f"pipecat-call-{stamp}.json"

    try:
        await asyncio.to_thread(client.connect_control)
        response = await asyncio.to_thread(client.dial, args.number)
        if response.get("status") != "ok":
            raise RuntimeError(f"dial failed: {response}")
        print("Dialing through Pipecat. Speak while the test tone is audible.", flush=True)
        await asyncio.to_thread(
            wait_for_state,
            client,
            {CallState.ACTIVE},
            timeout=args.active_timeout,
        )
        session.set_phase(SessionPhase.ACTIVE)
        await asyncio.to_thread(client.connect_media)
        runner_task = asyncio.create_task(runner.run(), name="pipecat-cellular-probe")
        await asyncio.wait_for(started.wait(), timeout=5.0)
        await asyncio.sleep(args.tone_delay)
        stale_before = int(
            (await asyncio.to_thread(client.get_audio_status))["audio"]["stale_uplink_frames"]
        )

        sink.tone_active = True
        total_frames = round(args.tone_seconds * 1000 / FRAME_MS)
        for frame_index in range(total_frames):
            payload = tone_frame(args.tone_frequency, args.tone_amplitude, frame_index)
            await worker.queue_frame(
                OutputAudioRawFrame(audio=payload, sample_rate=SAMPLE_RATE, num_channels=1)
            )
            output_frames += 1
            if sink.caller_speech.is_set():
                break
            await asyncio.sleep(FRAME_MS / 1000)

        if not sink.caller_speech.is_set():
            raise RuntimeError("Pipecat did not detect caller speech while the tone was active")

        cancelled_generation = session.generation_id
        await worker.queue_frame(InterruptionWorkerFrame())
        await wait_for_flush(session, cancelled_generation)
        flush_generation = session.generation_id
        if sink.onset_ns:
            onset_to_ack = (time.monotonic_ns() - sink.onset_ns) / 1_000_000

        stale_payload = tone_frame(args.tone_frequency, args.tone_amplitude, total_frames + 1)
        stale_sequence = session.snapshot().output_sequence + 10_000
        await asyncio.to_thread(
            client.link.send_audio_chunk,
            stale_payload,
            cancelled_generation,
            stale_sequence,
        )
        await asyncio.sleep(0.25)
        stale_after = int(
            (await asyncio.to_thread(client.get_audio_status))["audio"]["stale_uplink_frames"]
        )

        await asyncio.to_thread(write_wav, wav_path, bytes(sink.audio))

        result = PipecatProbeResult(
            call_id=str(session.call_id),
            link_epoch=str(session.link_epoch),
            pipeline_started=started.is_set(),
            pipecat_input_frames=sink.input_frames,
            pipecat_output_frames=output_frames,
            downlink_bytes=len(sink.audio),
            peak_dbfs=sink.peak_dbfs,
            flush_generation=flush_generation,
            onset_to_flush_ack_ms=onset_to_ack,
            stale_before=stale_before,
            stale_after=stale_after,
            stale_rejection_proven=stale_after > stale_before,
            wav_path=str(wav_path),
        )
        await asyncio.to_thread(
            json_path.write_text,
            json.dumps(asdict(result), indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(asdict(result), indent=2), flush=True)
        return result
    finally:
        if runner_task is not None:
            try:
                await worker.stop_when_done()
                await asyncio.wait_for(runner_task, timeout=5.0)
            except Exception:
                await runner.cancel("probe cleanup")
                await asyncio.gather(runner_task, return_exceptions=True)
        try:
            status = await asyncio.to_thread(client.get_status)
            if status.state not in {CallState.IDLE, CallState.DISCONNECTED}:
                await asyncio.to_thread(client.hangup)
        except Exception:
            pass
        await asyncio.to_thread(client.close)
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
    parser.add_argument("--output-dir", default="artifacts/pipecat-calls")
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
        result = asyncio.run(run(parse_args()))
    except Exception as exc:
        print(f"Pipecat call probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    if not result.stale_rejection_proven:
        raise SystemExit("Android did not reject the cancelled-generation Pipecat audio")


if __name__ == "__main__":
    main()
