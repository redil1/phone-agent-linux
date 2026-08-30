"""Content-free Deepgram Flux turn timing and revision tests."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest
from phone_agent_gateway.ai_bridge.telemetry import (
    CallTelemetry,
    FluxTurnState,
    FluxTurnTimingTracker,
)


class ManualClock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeFluxService:
    def __init__(self) -> None:
        self.handlers: dict[str, Callable[..., Awaitable[None]]] = {}

    def event_handler(self, name: str):
        def register(handler: Callable[..., Awaitable[None]]):
            self.handlers[name] = handler
            return handler

        return register

    async def emit(self, name: str, *args) -> None:
        await self.handlers[name](self, *args)


def event(snapshot: list[dict], name: str) -> dict:
    return next(item["values"] for item in snapshot if item["name"] == name)


def test_flux_tracker_records_matching_eager_window_without_transcript_content() -> None:
    clock = ManualClock()
    telemetry = CallTelemetry()
    tracker = FluxTurnTimingTracker(telemetry, clock=clock)

    tracker.start("please check")
    clock.advance(0.12)
    tracker.update("please check my order")
    clock.advance(0.08)
    tracker.eager("please check my order")
    clock.advance(0.14)
    tracker.commit("please check my order")

    assert tracker.state is FluxTurnState.COMMITTED
    final = event(telemetry.snapshot(), "stt_final_eot")
    assert final["eager_matches_final"] is True
    assert final["eager_to_final_ms"] == pytest.approx(140.0)
    assert final["since_start_ms"] == pytest.approx(340.0)
    serialized = json.dumps(telemetry.snapshot())
    assert "please" not in serialized
    assert "order" not in serialized


def test_flux_tracker_invalidates_eager_revision_when_speech_resumes() -> None:
    clock = ManualClock()
    telemetry = CallTelemetry()
    tracker = FluxTurnTimingTracker(telemetry, clock=clock)

    tracker.start()
    tracker.eager("send it Friday")
    eager_revision = tracker.revision
    clock.advance(0.05)
    tracker.resume()
    clock.advance(0.10)
    tracker.commit("send it Friday morning")

    assert tracker.state is FluxTurnState.COMMITTED
    assert tracker.revision > eager_revision
    resumed = event(telemetry.snapshot(), "stt_turn_resumed")
    final = event(telemetry.snapshot(), "stt_final_eot")
    assert resumed["had_eager"] is True
    assert final["eager_matches_final"] is False
    assert final["eager_to_final_ms"] is None


@pytest.mark.asyncio
async def test_flux_tracker_binds_to_public_service_events() -> None:
    clock = ManualClock()
    telemetry = CallTelemetry()
    tracker = FluxTurnTimingTracker(telemetry, clock=clock)
    service = FakeFluxService()
    tracker.bind(service)

    await service.emit("on_start_of_turn", "hello")
    clock.advance(0.1)
    await service.emit("on_update", "hello there")
    clock.advance(0.1)
    await service.emit("on_eager_end_of_turn", "hello there")
    clock.advance(0.1)
    await service.emit("on_end_of_turn", "hello there")

    assert tracker.turn == 1
    assert tracker.state is FluxTurnState.COMMITTED
    assert [item["name"] for item in telemetry.snapshot()] == [
        "stt_turn_started",
        "stt_partial_revision",
        "stt_eager_eot",
        "stt_final_eot",
    ]
