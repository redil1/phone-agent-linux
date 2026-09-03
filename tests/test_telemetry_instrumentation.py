"""Tests for Observability, Analytics, and Telemetry (Milestone 13)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.telemetry_instrumentation import (
    PlatformTelemetryHub,
    TurnTraceSpan,
)


def test_telemetry_span_recording_and_correlation() -> None:
    hub = PlatformTelemetryHub()
    span = TurnTraceSpan(
        trace_id="tr_123",
        call_id="call_abc",
        turn_id="turn_1",
        vad_ms=25.0,
        stt_ms=180.0,
        llm_ttft_ms=210.0,
        tts_first_audio_ms=95.0,
        total_latency_ms=510.0,
        outcome_status="success",
    )
    hub.record_span(span)
    spans = hub.get_spans_for_call("call_abc")
    assert len(spans) == 1
    assert spans[0].total_latency_ms == 510.0
    assert spans[0].llm_ttft_ms == 210.0
