"""Offline unit tests for the Mac gateway client contract."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from phone_agent_gateway.mac_client.gateway_client import CallState, CallStatus, PhoneAgentClient


class RecordingClient(PhoneAgentClient):
    def __init__(self) -> None:
        super().__init__(auto_forward_adb=False)
        self.requests: list[tuple[str, dict | None]] = []

    def _request(self, endpoint: str, data: dict | None = None) -> dict:
        self.requests.append((endpoint, data))
        return {"status": "ok", "action": "recorded"}


def test_call_status_accepts_extended_gateway_states() -> None:
    for state in ("DIALING", "CONNECTING", "ACTIVE", "HOLDING", "DISCONNECTED"):
        parsed = CallStatus.from_dict(
            {"status": "ok", "state": state, "state_code": 1, "incoming_number": ""}
        )
        assert parsed.state == CallState(state)


def test_dial_normalizes_moroccan_international_prefix() -> None:
    client = RecordingClient()
    client.dial("+212 6-12-34-56-78")
    assert client.requests == [("/call/dial", {"number": "0612345678"})]


def test_audio_flush_uses_control_endpoint() -> None:
    client = RecordingClient()
    client.flush_audio()
    assert client.requests == [("/audio/flush", {})]
