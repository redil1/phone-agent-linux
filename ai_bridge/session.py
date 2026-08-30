"""Per-call state, generation ownership, and conversation coordination."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from uuid import UUID, uuid4


class SessionError(RuntimeError):
    """Invalid call-session operation."""


class SessionPhase(StrEnum):
    CREATED = "CREATED"
    CONNECTING = "CONNECTING"
    ACTIVE = "ACTIVE"
    ENDING = "ENDING"
    CLOSED = "CLOSED"


class TurnPhase(StrEnum):
    LISTENING = "LISTENING"
    USER_SPEAKING = "USER_SPEAKING"
    TURN_CANDIDATE_END = "TURN_CANDIDATE_END"
    THINKING = "THINKING"
    TOOL_WAIT = "TOOL_WAIT"
    SPEAKING = "SPEAKING"
    CANCELLING = "CANCELLING"


@dataclass(frozen=True, slots=True)
class GenerationAdvance:
    call_id: UUID
    cancelled_generation: int
    next_generation: int
    reason: str
    monotonic_ns: int


@dataclass(slots=True)
class SessionMetrics:
    input_frames: int = 0
    output_frames: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    dropped_input_frames: int = 0
    dropped_output_frames: int = 0
    stale_input_frames: int = 0
    stale_output_frames: int = 0
    sequence_gaps: int = 0
    interruptions: int = 0
    flush_failures: int = 0
    last_flush_latency_ms: float | None = None
    last_output_sequence: int = -1
    last_rendered_sequence: int = -1

    def to_dict(self) -> dict[str, int | float | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    call_id: UUID
    link_epoch: UUID
    phase: SessionPhase
    turn_phase: TurnPhase
    generation_id: int
    input_sequence: int
    output_sequence: int
    metrics: dict[str, int | float | None]


class CallSessionState:
    """Thread-safe state shared by socket callbacks and the async pipeline."""

    def __init__(self, call_id: UUID | None = None) -> None:
        self._lock = threading.RLock()
        self._call_id = call_id or uuid4()
        self._link_epoch = uuid4()
        self._phase = SessionPhase.CREATED
        self._turn_phase = TurnPhase.LISTENING
        self._generation_id = 1
        self._input_sequence = -1
        self._output_sequence = -1
        self.metrics = SessionMetrics()

    @property
    def call_id(self) -> UUID:
        return self._call_id

    @property
    def generation_id(self) -> int:
        with self._lock:
            return self._generation_id

    @property
    def link_epoch(self) -> UUID:
        with self._lock:
            return self._link_epoch

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._phase is SessionPhase.ACTIVE

    def set_phase(self, phase: SessionPhase) -> None:
        with self._lock:
            allowed = {
                SessionPhase.CREATED: {SessionPhase.CONNECTING, SessionPhase.CLOSED},
                SessionPhase.CONNECTING: {SessionPhase.ACTIVE, SessionPhase.ENDING},
                SessionPhase.ACTIVE: {SessionPhase.ENDING},
                SessionPhase.ENDING: {SessionPhase.CLOSED},
                SessionPhase.CLOSED: set(),
            }
            if phase is self._phase:
                return
            if phase not in allowed[self._phase]:
                raise SessionError(f"invalid session transition {self._phase} -> {phase}")
            self._phase = phase

    def set_turn_phase(self, phase: TurnPhase) -> None:
        with self._lock:
            if self._phase is not SessionPhase.ACTIVE:
                raise SessionError("turn state can change only while the call is active")
            self._turn_phase = phase

    def reconnect(self) -> UUID:
        """Start a new link epoch without changing the call or generation."""

        with self._lock:
            if self._phase in (SessionPhase.ENDING, SessionPhase.CLOSED):
                raise SessionError("cannot reconnect a closing session")
            self._link_epoch = uuid4()
            self._input_sequence = -1
            self._output_sequence = -1
            return self._link_epoch

    def resynchronize_generation(self, minimum_generation: int) -> int:
        """Advance to the phone's generation after an authenticated reconnect.

        Generation is monotonic. A reconnect may discover that Android already
        processed a flush acknowledgement which the Mac did not receive. It is
        safe to advance to that value, but never to move backwards and revive
        cancelled audio.
        """

        if minimum_generation < 1:
            raise SessionError("minimum_generation must be >= 1")
        with self._lock:
            if minimum_generation > self._generation_id:
                self._generation_id = minimum_generation
                self._input_sequence = -1
                self._output_sequence = -1
            return self._generation_id

    def next_output_identity(self) -> tuple[int, int]:
        with self._lock:
            if self._phase is not SessionPhase.ACTIVE:
                raise SessionError("refusing output while call is not ACTIVE")
            self._output_sequence += 1
            return self._generation_id, self._output_sequence

    def accept_input_sequence(self, generation_id: int, sequence: int, payload_bytes: int) -> bool:
        """Validate an inbound frame and account for sequence gaps."""

        with self._lock:
            if self._phase is not SessionPhase.ACTIVE:
                self.metrics.stale_input_frames += 1
                return False
            if generation_id != self._generation_id:
                self.metrics.stale_input_frames += 1
                return False
            if sequence <= self._input_sequence:
                self.metrics.stale_input_frames += 1
                return False
            if self._input_sequence >= 0 and sequence > self._input_sequence + 1:
                self.metrics.sequence_gaps += sequence - self._input_sequence - 1
            self._input_sequence = sequence
            self.metrics.input_frames += 1
            self.metrics.input_bytes += payload_bytes
            return True

    def account_legacy_input(self, payload_bytes: int) -> int:
        """Assign sequence numbers to the current raw-socket compatibility path."""

        with self._lock:
            self._input_sequence += 1
            self.metrics.input_frames += 1
            self.metrics.input_bytes += payload_bytes
            return self._input_sequence

    def account_output(self, generation_id: int, sequence: int, payload_bytes: int) -> bool:
        with self._lock:
            if self._phase is not SessionPhase.ACTIVE or generation_id != self._generation_id:
                self.metrics.stale_output_frames += 1
                return False
            self.metrics.output_frames += 1
            self.metrics.output_bytes += payload_bytes
            self.metrics.last_output_sequence = sequence
            return True

    def mark_rendered(self, generation_id: int, sequence: int) -> bool:
        with self._lock:
            if generation_id != self._generation_id:
                self.metrics.stale_output_frames += 1
                return False
            self.metrics.last_rendered_sequence = max(
                self.metrics.last_rendered_sequence, sequence
            )
            return True

    def interrupt(self, reason: str) -> GenerationAdvance:
        with self._lock:
            cancelled = self._generation_id
            self._generation_id += 1
            self._turn_phase = TurnPhase.CANCELLING
            self.metrics.interruptions += 1
            return GenerationAdvance(
                call_id=self._call_id,
                cancelled_generation=cancelled,
                next_generation=self._generation_id,
                reason=reason,
                monotonic_ns=time.monotonic_ns(),
            )

    def finish_interruption(self) -> None:
        with self._lock:
            if self._phase is SessionPhase.ACTIVE:
                self._turn_phase = TurnPhase.LISTENING

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            return SessionSnapshot(
                call_id=self._call_id,
                link_epoch=self._link_epoch,
                phase=self._phase,
                turn_phase=self._turn_phase,
                generation_id=self._generation_id,
                input_sequence=self._input_sequence,
                output_sequence=self._output_sequence,
                metrics=self.metrics.to_dict(),
            )


FlushCallback = Callable[[GenerationAdvance], Awaitable[object] | object]


class ConversationCoordinator:
    """Owns the single response/cancellation scope for one call.

    Pipecat owns frame propagation. This object owns project-specific response
    tasks (retrieval, tools, speculation) so no auxiliary task can keep
    producing content after the caller interrupts.
    """

    def __init__(self, session: CallSessionState) -> None:
        self.session = session
        self._lock = asyncio.Lock()
        self._response_tasks: set[asyncio.Task] = set()

    async def register_response_task(self, task: asyncio.Task) -> None:
        async with self._lock:
            self._response_tasks.add(task)
            task.add_done_callback(self._response_tasks.discard)

    async def interrupt(self, reason: str, flush: FlushCallback) -> GenerationAdvance:
        advance = self.session.interrupt(reason)
        async with self._lock:
            tasks = tuple(self._response_tasks)
            self._response_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        started = time.monotonic()
        try:
            result = flush(advance)
            if inspect.isawaitable(result):
                await result
        except Exception:
            self.session.metrics.flush_failures += 1
            raise
        finally:
            self.session.metrics.last_flush_latency_ms = (time.monotonic() - started) * 1000
            self.session.finish_interruption()
        return advance
