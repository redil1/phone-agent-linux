"""Bounded per-call latency and turn telemetry.

No audio or transcript content is stored here. The default metrics surface is
intentionally metadata-only so production diagnostics do not silently become
call recording.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from pipecat.observers.turn_tracking_observer import TurnTrackingObserver
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver

logger = logging.getLogger("PhoneAgentTelemetry")


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    name: str
    values: dict[str, Any]


class CallTelemetry:
    """Collect a bounded, content-free record of important call measurements."""

    def __init__(self, max_events: int = 512) -> None:
        self._events: deque[TelemetryEvent] = deque(maxlen=max_events)
        self.latency = UserBotLatencyObserver()
        self.turns = TurnTrackingObserver(turn_end_timeout_secs=1.0)
        self._register_handlers()

    @property
    def observers(self) -> list:
        return [self.latency, self.turns]

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(event) for event in self._events]

    def record(self, name: str, **values: Any) -> None:
        """Record one metadata-only event from a project-specific processor."""

        self._record(name, **values)

    def _record(self, name: str, **values: Any) -> None:
        event = TelemetryEvent(name=name, values=values)
        self._events.append(event)
        logger.info("voice_metric name=%s values=%s", name, values)

    def _register_handlers(self) -> None:
        @self.latency.event_handler("on_latency_measured")
        async def on_latency_measured(_observer, latency_seconds: float) -> None:
            self._record("user_to_bot_latency", milliseconds=latency_seconds * 1000)

        @self.latency.event_handler("on_first_bot_speech_latency")
        async def on_first_bot_speech(_observer, latency_seconds: float) -> None:
            self._record("first_bot_speech_latency", milliseconds=latency_seconds * 1000)

        @self.latency.event_handler("on_latency_breakdown")
        async def on_latency_breakdown(_observer, breakdown) -> None:
            # Pipecat owns this schema. Preserve its readable ordered event list
            # rather than coupling the gateway to private metric internals.
            values = (
                breakdown.ordered_events()
                if hasattr(breakdown, "ordered_events")
                else [str(breakdown)]
            )
            self._record("latency_breakdown", events=values)

        @self.turns.event_handler("on_turn_started")
        async def on_turn_started(_observer, turn_number: int) -> None:
            self._record("turn_started", turn=turn_number)

        @self.turns.event_handler("on_turn_ended")
        async def on_turn_ended(
            _observer, turn_number: int, duration: float, was_interrupted: bool
        ) -> None:
            self._record(
                "turn_ended",
                turn=turn_number,
                duration_ms=duration * 1000,
                interrupted=was_interrupted,
            )


class FluxTurnState(StrEnum):
    """Observable Deepgram Flux turn states; no transcript content is retained."""

    LISTENING = "LISTENING"
    EAGER = "EAGER"
    RESUMED = "RESUMED"
    COMMITTED = "COMMITTED"


class FluxTurnTimingTracker:
    """Track Flux turn revisions and timing without changing response behavior.

    Pipecat 1.7 exposes EagerEndOfTurn as an interim transcript, but does not
    provide a safe speculative-output commit gate. This tracker measures the
    real eager-to-final window first. Transcript text is held only for the
    current turn so eager/final equality can be checked; telemetry stores only
    lengths, counters, booleans, and durations.
    """

    def __init__(
        self,
        telemetry: CallTelemetry,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_partial_events: int = 8,
    ) -> None:
        self._telemetry = telemetry
        self._clock = clock
        self._max_partial_events = max_partial_events
        self._turn = 0
        self._revision = 0
        self._state = FluxTurnState.LISTENING
        self._started_at: float | None = None
        self._first_update_at: float | None = None
        self._eager_at: float | None = None
        self._eager_text: str | None = None
        self._latest_text = ""
        self._update_count = 0

    @property
    def state(self) -> FluxTurnState:
        return self._state

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def revision(self) -> int:
        return self._revision

    def bind(self, service: Any) -> None:
        """Bind to the public Deepgram Flux service event surface."""

        @service.event_handler("on_start_of_turn")
        async def on_start_of_turn(_service, transcript: str) -> None:
            self.start(transcript)

        @service.event_handler("on_update")
        async def on_update(_service, transcript: str) -> None:
            self.update(transcript)

        @service.event_handler("on_eager_end_of_turn")
        async def on_eager_end_of_turn(_service, transcript: str) -> None:
            self.eager(transcript)

        @service.event_handler("on_turn_resumed")
        async def on_turn_resumed(_service) -> None:
            self.resume()

        @service.event_handler("on_end_of_turn")
        async def on_end_of_turn(_service, transcript: str) -> None:
            self.commit(transcript)

    def start(self, transcript: str = "") -> None:
        now = self._clock()
        text = self._normalize(transcript)
        self._turn += 1
        self._revision = 1 if text else 0
        self._state = FluxTurnState.LISTENING
        self._started_at = now
        self._first_update_at = None
        self._eager_at = None
        self._eager_text = None
        self._latest_text = text
        self._update_count = 0
        self._telemetry.record(
            "stt_turn_started",
            turn=self._turn,
            revision=self._revision,
            initial_chars=len(text),
        )

    def update(self, transcript: str) -> None:
        text = self._normalize(transcript)
        if not text or text == self._latest_text:
            return
        now = self._clock()
        self._ensure_started(now)
        self._revision += 1
        self._update_count += 1
        self._latest_text = text
        if self._first_update_at is None:
            self._first_update_at = now
        if self._update_count <= self._max_partial_events:
            self._telemetry.record(
                "stt_partial_revision",
                turn=self._turn,
                revision=self._revision,
                chars=len(text),
                since_start_ms=self._elapsed_ms(self._started_at, now),
            )

    def eager(self, transcript: str) -> None:
        now = self._clock()
        text = self._normalize(transcript)
        self._ensure_started(now)
        if text and text != self._latest_text:
            self._revision += 1
            self._latest_text = text
        self._state = FluxTurnState.EAGER
        self._eager_at = now
        self._eager_text = text
        self._telemetry.record(
            "stt_eager_eot",
            turn=self._turn,
            revision=self._revision,
            chars=len(text),
            since_start_ms=self._elapsed_ms(self._started_at, now),
        )

    def resume(self) -> None:
        now = self._clock()
        self._ensure_started(now)
        had_eager = self._eager_at is not None
        self._state = FluxTurnState.RESUMED
        self._revision += 1
        self._telemetry.record(
            "stt_turn_resumed",
            turn=self._turn,
            revision=self._revision,
            had_eager=had_eager,
            eager_active_ms=self._elapsed_ms(self._eager_at, now),
        )
        self._eager_at = None
        self._eager_text = None

    def commit(self, transcript: str) -> None:
        now = self._clock()
        text = self._normalize(transcript)
        self._ensure_started(now)
        if text and text != self._latest_text:
            self._revision += 1
            self._latest_text = text
        eager_matches = self._eager_text is not None and self._eager_text == text
        self._state = FluxTurnState.COMMITTED
        self._telemetry.record(
            "stt_final_eot",
            turn=self._turn,
            revision=self._revision,
            chars=len(text),
            since_start_ms=self._elapsed_ms(self._started_at, now),
            first_update_ms=self._elapsed_ms(self._started_at, self._first_update_at),
            eager_to_final_ms=self._elapsed_ms(self._eager_at, now),
            eager_matches_final=eager_matches,
            partial_revisions=self._update_count,
        )

    def _ensure_started(self, now: float) -> None:
        if self._started_at is not None:
            return
        self._turn += 1
        self._started_at = now

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", text).split())

    @staticmethod
    def _elapsed_ms(start: float | None, end: float | None) -> float | None:
        if start is None or end is None:
            return None
        return max(0.0, (end - start) * 1000)
