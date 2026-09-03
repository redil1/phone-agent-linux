"""OpenTelemetry and distributed trace telemetry for Universal Cascade.

Governed by Milestone 13 (M13-01 through M13-10):
Links call_id, turn_id, audio epoch, latency phases, and outcome attribution.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TurnTraceSpan(StrictModel):
    trace_id: str
    call_id: str
    turn_id: str
    vad_ms: float = 0.0
    stt_ms: float = 0.0
    llm_ttft_ms: float = 0.0
    tts_first_audio_ms: float = 0.0
    total_latency_ms: float = 0.0
    outcome_status: str = "success"


class PlatformTelemetryHub:
    """Central telemetry and trace manager (M13-01, M13-02, M13-10)."""

    def __init__(self) -> None:
        self.spans: list[TurnTraceSpan] = []

    def record_span(self, span: TurnTraceSpan) -> None:
        self.spans.append(span)

    def get_spans_for_call(self, call_id: str) -> list[TurnTraceSpan]:
        return [s for s in self.spans if s.call_id == call_id]
