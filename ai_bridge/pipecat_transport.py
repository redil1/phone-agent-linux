"""Production Pipecat transport for the PhoneAgent cellular audio bridge.

Pipecat owns ordered frame flow and cancellation. This transport owns the
project-specific boundary: exact 20 ms PCM frames, bounded ingress, real-time
Android-clock flow control, call/generation validation, and urgent playback flush.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pydantic import Field

from .media_protocol import FrameDirection as WireDirection
from .media_protocol import FrameKind, MediaFrame
from .session import CallSessionState, ConversationCoordinator, GenerationAdvance, SessionError

logger = logging.getLogger("PhoneAgentPipecatTransport")

PCM_SAMPLE_WIDTH = 2
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
DEFAULT_FRAME_MS = 20

TxHandler = Callable[[bytes, int, int], object | Awaitable[object]]
FlushHandler = Callable[[GenerationAdvance], object | Awaitable[object]]
AudioEndHandler = Callable[[int, int], object | Awaitable[object]]
AudioListener = Callable[[bytes], None]


class AudioWriteResult(StrEnum):
    """Outcome of one phone-output write.

    ``CANCELLED`` is normal during barge-in: the generation changed while a
    blocking link write was in flight. It must not be promoted to a dead-link
    error or terminate the long-lived Realtime receiver.
    """

    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class PhoneAudioEndFrame(Frame):
    """Ordered internal marker placed behind all audio for one TTS segment."""


class PhoneAgentTransportParams(TransportParams):
    """Validated transport and latency bounds."""

    audio_in_enabled: bool = True
    audio_out_enabled: bool = True
    audio_in_sample_rate: int | None = DEFAULT_SAMPLE_RATE
    audio_out_sample_rate: int | None = DEFAULT_SAMPLE_RATE
    audio_in_channels: int = DEFAULT_CHANNELS
    audio_out_channels: int = DEFAULT_CHANNELS
    audio_out_10ms_chunks: int = 2
    audio_out_auto_silence: bool = False
    audio_out_end_silence_secs: int = 0
    frame_ms: int = DEFAULT_FRAME_MS
    input_queue_frames: int = Field(default=25, ge=2, le=50)
    flush_timeout_secs: float = Field(default=5.0, gt=0.05, le=10.0)
    require_active_call: bool = True

    @property
    def input_frame_bytes(self) -> int:
        sample_rate = self.audio_in_sample_rate or DEFAULT_SAMPLE_RATE
        return sample_rate * self.frame_ms // 1000 * self.audio_in_channels * PCM_SAMPLE_WIDTH


async def _invoke_callback(callback: Callable, *args: Any) -> Any:
    """Run async callbacks directly and isolate blocking legacy callbacks."""

    if inspect.iscoroutinefunction(callback):
        return await callback(*args)
    result = await asyncio.to_thread(callback, *args)
    if inspect.isawaitable(result):
        return await result
    return result


class PhoneAgentInputTransport(BaseInputTransport):
    """Turn phone downlink bytes into bounded Pipecat audio frames."""

    def __init__(
        self,
        transport: PhoneAgentTransport,
        params: PhoneAgentTransportParams,
        session: CallSessionState,
        **kwargs: Any,
    ) -> None:
        super().__init__(params, **kwargs)
        self._transport = transport
        self._phone_params = params
        self._session = session
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ingress: asyncio.Queue[bytes] = asyncio.Queue(maxsize=params.input_queue_frames)
        self._pending: deque[bytes] = deque(maxlen=params.input_queue_frames)
        self._pcm_buffer = bytearray()
        self._feed_lock = threading.Lock()
        self._pump_task: asyncio.Task | None = None
        self._initialized = False

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        self._loop = asyncio.get_running_loop()
        await self.set_transport_ready(frame)
        self._pump_task = self.create_task(self._pump_audio())

        with self._feed_lock:
            pending = tuple(self._pending)
            self._pending.clear()
        for chunk in pending:
            self._offer_chunk(chunk)

    async def stop(self, frame: EndFrame) -> None:
        await self._stop_pump()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._stop_pump()
        await super().cancel(frame)

    async def cleanup(self) -> None:
        await self._stop_pump()
        await super().cleanup()

    def push_audio(self, pcm_bytes: bytes) -> None:
        """Thread-safe callback used by the current raw Android RX socket.

        TCP reads are not frame boundaries, so arbitrary chunks are accumulated
        and emitted only as exact 20 ms frames.
        """

        if not pcm_bytes:
            return
        chunks: list[bytes] = []
        frame_bytes = self._phone_params.input_frame_bytes
        with self._feed_lock:
            self._pcm_buffer.extend(pcm_bytes)
            while len(self._pcm_buffer) >= frame_bytes:
                chunks.append(bytes(self._pcm_buffer[:frame_bytes]))
                del self._pcm_buffer[:frame_bytes]

            loop = self._loop
            if loop is None or loop.is_closed():
                for chunk in chunks:
                    if len(self._pending) == self._pending.maxlen:
                        self._session.metrics.dropped_input_frames += 1
                    self._pending.append(chunk)
                return

        for chunk in chunks:
            self._session.account_legacy_input(len(chunk))
            try:
                loop.call_soon_threadsafe(self._offer_chunk, chunk)
            except RuntimeError:
                self._session.metrics.dropped_input_frames += 1

    def push_media_frame(self, frame: MediaFrame) -> bool:
        """Accept and enqueue one authenticated, already-decoded phone media frame."""

        return self.accept_media_frame(frame, enqueue=True)

    def accept_audio_bytes(self, pcm: bytes, *, enqueue: bool) -> bool:
        """Take caller audio that arrived without the cellular framing.

        Same accounting as a framed phone packet, minus the checks that only
        make sense for a sequenced, authenticated link.
        """

        if len(pcm) != self._phone_params.input_frame_bytes:
            self._session.metrics.dropped_input_frames += 1
            return False
        if not enqueue:
            return True

        with self._feed_lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                if len(self._pending) == self._pending.maxlen:
                    self._session.metrics.dropped_input_frames += 1
                self._pending.append(pcm)
                return True
        try:
            loop.call_soon_threadsafe(self._offer_chunk, pcm)
        except RuntimeError:
            self._session.metrics.dropped_input_frames += 1
            return False
        return True

    def accept_media_frame(self, frame: MediaFrame, *, enqueue: bool) -> bool:
        """Validate/account one phone frame, optionally feeding the Pipecat ingress."""

        if frame.kind is not FrameKind.AUDIO:
            raise ValueError("phone input accepts only AUDIO frames")
        if frame.direction is not WireDirection.PHONE_TO_MAC:
            raise ValueError("phone input frame has the wrong direction")
        if frame.call_id != self._session.call_id:
            self._session.metrics.stale_input_frames += 1
            return False
        if frame.sample_rate != self._phone_params.audio_in_sample_rate:
            self._session.metrics.dropped_input_frames += 1
            return False
        if frame.channels != self._phone_params.audio_in_channels or frame.sample_width != 2:
            self._session.metrics.dropped_input_frames += 1
            return False
        if len(frame.payload) != self._phone_params.input_frame_bytes:
            self._session.metrics.dropped_input_frames += 1
            return False
        if not self._session.accept_input_sequence(
            frame.generation_id, frame.sequence, len(frame.payload)
        ):
            return False
        if not enqueue:
            return True

        with self._feed_lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                if len(self._pending) == self._pending.maxlen:
                    self._session.metrics.dropped_input_frames += 1
                self._pending.append(frame.payload)
                return True
        try:
            loop.call_soon_threadsafe(self._offer_chunk, frame.payload)
        except RuntimeError:
            self._session.metrics.dropped_input_frames += 1
            return False
        return True

    def _offer_chunk(self, chunk: bytes) -> None:
        if self._ingress.full():
            try:
                self._ingress.get_nowait()
                self._ingress.task_done()
            except asyncio.QueueEmpty:
                pass
            self._session.metrics.dropped_input_frames += 1
        self._ingress.put_nowait(chunk)

    async def _pump_audio(self) -> None:
        while True:
            chunk = await self._ingress.get()
            try:
                if self._phone_params.require_active_call and not self._session.is_active:
                    self._session.metrics.dropped_input_frames += 1
                    continue
                await self.push_audio_frame(
                    InputAudioRawFrame(
                        audio=chunk,
                        sample_rate=self._phone_params.audio_in_sample_rate or DEFAULT_SAMPLE_RATE,
                        num_channels=self._phone_params.audio_in_channels,
                    )
                )
            finally:
                self._ingress.task_done()

    async def _stop_pump(self) -> None:
        self._loop = None
        if self._pump_task:
            await self.cancel_task(self._pump_task)
            self._pump_task = None
        while not self._ingress.empty():
            try:
                self._ingress.get_nowait()
                self._ingress.task_done()
            except asyncio.QueueEmpty:
                break


class PhoneAgentOutputTransport(BaseOutputTransport):
    """Flow-control Pipecat output into the phone and flush it on interruption."""

    def __init__(
        self,
        transport: PhoneAgentTransport,
        params: PhoneAgentTransportParams,
        session: CallSessionState,
        coordinator: ConversationCoordinator,
        **kwargs: Any,
    ) -> None:
        super().__init__(params, **kwargs)
        self._transport = transport
        self._phone_params = params
        self._session = session
        self._coordinator = coordinator
        self._tx_handler: TxHandler | None = None
        self._audio_end_handler: AudioEndHandler | None = None
        self._flush_handler: FlushHandler | None = None
        self._initialized = False
        self._audio_since_last_end = False
        self._audio_end_epoch = 0
        self._last_audio_end_identity: tuple[int, int] | None = None
        self._audio_end_available = asyncio.Event()

    @property
    def audio_end_epoch(self) -> int:
        return self._audio_end_epoch

    async def wait_for_audio_end(
        self,
        after_epoch: int,
        *,
        timeout_secs: float = 1.0,
    ) -> tuple[int, int] | None:
        """Return the ordered end marker created after one speaking span."""

        deadline = asyncio.get_running_loop().time() + timeout_secs
        while self._audio_end_epoch <= after_epoch:
            self._audio_end_available.clear()
            if self._audio_end_epoch > after_epoch:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._audio_end_available.wait(), timeout=remaining)
            except TimeoutError:
                return None
        return self._last_audio_end_identity

    @property
    def generation_id(self) -> int:
        return self._session.generation_id

    def set_tx_handler(self, callback: TxHandler) -> None:
        self._tx_handler = callback

    def set_audio_end_handler(self, callback: AudioEndHandler) -> None:
        self._audio_end_handler = callback

    def set_flush_handler(self, callback: FlushHandler) -> None:
        self._flush_handler = callback

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        await self.set_transport_ready(frame)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        # Crucial: explicitly drop any InputAudioRawFrame to guarantee ZERO software echo
        if isinstance(frame, InputAudioRawFrame):
            return

        interrupt_task: asyncio.Task | None = None
        if isinstance(frame, InterruptionFrame):
            # Start the phone flush on the urgent path while Pipecat clears its
            # own output queues. Neither operation waits behind media.
            interrupt_task = asyncio.create_task(
                self._coordinator.interrupt("pipecat_user_interruption", self._flush_phone)
            )

        try:
            await super().process_frame(frame, direction)
        finally:
            if interrupt_task is not None:
                await interrupt_task

        # BaseOutputTransport queues TTSStoppedFrame behind all pending audio.
        # Queue our private marker immediately after it so the media sender
        # invokes write_transport_frame only when every preceding PCM frame has
        # reached the authenticated phone link.
        if isinstance(frame, TTSStoppedFrame) and direction is FrameDirection.DOWNSTREAM:
            await super().process_frame(PhoneAudioEndFrame(), direction)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        return await self.write_audio_frame_result(frame) is AudioWriteResult.DELIVERED

    async def write_audio_frame_result(self, frame: OutputAudioRawFrame) -> AudioWriteResult:
        """Write PCM and distinguish expected interruption from link failure."""

        if isinstance(frame, InputAudioRawFrame):
            return AudioWriteResult.FAILED
        if self._tx_handler is None:
            self._session.metrics.dropped_output_frames += 1
            return AudioWriteResult.FAILED

        # Edge already returns clean, normalized speech. Previous per-chunk filtering and
        # gain normalization reset DSP state at arbitrary decoder boundaries, producing
        # clicks, pumping, and near-clipped speech. Preserve the source PCM exactly here;
        # the cellular codec performs its own required band limiting.
        payload = frame.audio
        if not payload:
            return AudioWriteResult.DELIVERED

        chunk_size = self._phone_params.input_frame_bytes  # Exactly 640 bytes required by Android

        for offset in range(0, len(payload), chunk_size):
            chunk = payload[offset : offset + chunk_size]
            if len(chunk) < chunk_size:
                # Pad final chunk with silence to maintain exact 640 bytes for Android bridge
                chunk = chunk + b"\x00" * (chunk_size - len(chunk))

            try:
                generation_id, sequence = self._session.next_output_identity()
            except SessionError:
                self._session.metrics.dropped_output_frames += 1
                return AudioWriteResult.FAILED

            # Do not pace from Python. Android's AudioTrack is the one playout
            # clock; the bounded phone queue and TCP backpressure absorb this
            # ready audio burst without allowing unbounded cancellation lag.
            if generation_id != self._session.generation_id:
                self._session.metrics.stale_output_frames += 1
                return AudioWriteResult.CANCELLED

            try:
                await _invoke_callback(self._tx_handler, chunk, generation_id, sequence)
            except Exception as exc:
                if generation_id != self._session.generation_id:
                    self._session.metrics.stale_output_frames += 1
                    logger.info(
                        "discarded in-flight phone frame from interrupted generation=%d",
                        generation_id,
                    )
                    return AudioWriteResult.CANCELLED
                self._session.metrics.dropped_output_frames += 1
                logger.warning("phone uplink write failed: %s", exc)
                return AudioWriteResult.FAILED

            if not self._session.account_output(generation_id, sequence, len(chunk)):
                if generation_id != self._session.generation_id:
                    return AudioWriteResult.CANCELLED
                return AudioWriteResult.FAILED
            self._transport.notify_output_audio(chunk)
            self._audio_since_last_end = True

        return AudioWriteResult.DELIVERED

    async def write_transport_frame(self, frame: Frame) -> None:
        if not isinstance(frame, PhoneAudioEndFrame) or self._audio_end_handler is None:
            return
        await self.finish_audio_segment()

    async def finish_audio_segment(self) -> tuple[int, int] | None:
        """Queue an ordered end marker and return its phone-render identity.

        The authenticated uplink acknowledges this marker only after Android's
        AudioTrack has rendered every preceding audio frame. S2S uses the
        identity to report what the remote caller actually heard, rather than
        treating a generated transcript as successful playback.
        """
        if self._audio_end_handler is None:
            return None
        if not self._audio_since_last_end:
            logger.warning("Skipped phone audio end marker because TTS produced no PCM")
            return None
        self._audio_since_last_end = False
        try:
            generation_id, sequence = self._session.next_output_identity()
        except SessionError:
            return None
        if generation_id != self._session.generation_id:
            return None
        await _invoke_callback(self._audio_end_handler, generation_id, sequence)
        self._last_audio_end_identity = (generation_id, sequence)
        self._audio_end_epoch += 1
        self._audio_end_available.set()
        return generation_id, sequence

    def discard_audio_segment(self) -> None:
        """Forget buffered segment bookkeeping after an urgent interruption."""

        self._audio_since_last_end = False

    async def _flush_phone(self, advance: GenerationAdvance) -> Any:
        if self._flush_handler is None:
            raise RuntimeError("phone flush handler is not configured")

        result = await asyncio.wait_for(
            _invoke_callback(self._flush_handler, advance),
            timeout=self._phone_params.flush_timeout_secs,
        )
        if isinstance(result, dict) and result.get("status") not in (None, "ok"):
            raise RuntimeError(f"phone rejected audio flush: {result}")
        logger.info(
            "flushed phone output generation %d -> %d",
            advance.cancelled_generation,
            advance.next_generation,
        )
        return result


class PhoneAgentTransport(BaseTransport):
    """Pipecat transport container for one isolated cellular call."""

    def __init__(
        self,
        params: PhoneAgentTransportParams | None = None,
        *,
        session: CallSessionState | None = None,
        input_name: str | None = None,
        output_name: str | None = None,
    ) -> None:
        super().__init__(input_name=input_name, output_name=output_name)
        self.params = params or PhoneAgentTransportParams()
        self.session = session or CallSessionState()
        self.coordinator = ConversationCoordinator(self.session)
        self._input: PhoneAgentInputTransport | None = None
        self._output: PhoneAgentOutputTransport | None = None
        self._audio_listeners: list[AudioListener] = []
        self._output_audio_listeners: list[AudioListener] = []

    def add_audio_listener(self, listener: AudioListener) -> None:
        if listener not in self._audio_listeners:
            self._audio_listeners.append(listener)

    def remove_audio_listener(self, listener: AudioListener) -> None:
        if listener in self._audio_listeners:
            self._audio_listeners.remove(listener)

    def add_output_audio_listener(self, listener: AudioListener) -> None:
        if listener not in self._output_audio_listeners:
            self._output_audio_listeners.append(listener)

    def remove_output_audio_listener(self, listener: AudioListener) -> None:
        if listener in self._output_audio_listeners:
            self._output_audio_listeners.remove(listener)

    def notify_output_audio(self, pcm_bytes: bytes) -> None:
        for listener in self._output_audio_listeners:
            try:
                listener(pcm_bytes)
            except Exception:
                logger.warning("phone output audio listener failed", exc_info=True)

    def input(self) -> PhoneAgentInputTransport:
        if self._input is None:
            self._input = PhoneAgentInputTransport(
                self,
                self.params,
                self.session,
                name=self._input_name,
            )
        return self._input

    def output(self) -> PhoneAgentOutputTransport:
        if self._output is None:
            self._output = PhoneAgentOutputTransport(
                self,
                self.params,
                self.session,
                self.coordinator,
                name=self._output_name,
            )
        return self._output

    def feed_phone_audio(self, pcm_bytes: bytes) -> None:
        for listener in self._audio_listeners:
            try:
                listener(pcm_bytes)
            except Exception:
                pass
        self.input().push_audio(pcm_bytes)

    def feed_phone_frame(self, frame: MediaFrame) -> None:
        if self.input().push_media_frame(frame) and frame.payload:
            for listener in self._audio_listeners:
                try:
                    listener(frame.payload)
                except Exception:
                    pass

    def feed_audio_bytes(self, pcm: bytes) -> None:
        """Unframed caller audio for the cascade pipeline's ingress queue.

        The speech recogniser reads from the Pipecat queue, not from the audio
        listeners, so a channel that only notified listeners left STT deaf: the
        greeting played and the call then sat in silence.
        """

        if not pcm:
            return
        for listener in self._audio_listeners:
            try:
                listener(pcm)
            except Exception:
                logger.warning("audio listener failed", exc_info=True)
        self.input().accept_audio_bytes(pcm, enqueue=True)


    def set_tx_handler(self, callback: TxHandler) -> None:
        self.output().set_tx_handler(callback)

    def set_audio_end_handler(self, callback: AudioEndHandler) -> None:
        self.output().set_audio_end_handler(callback)

    def set_flush_handler(self, callback: FlushHandler) -> None:
        self.output().set_flush_handler(callback)

    @property
    def input_processor(self) -> FrameProcessor:
        return self.input()

    @property
    def output_processor(self) -> FrameProcessor:
        return self.output()
