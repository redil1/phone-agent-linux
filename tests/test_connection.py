"""Unit & Integration tests for PhoneAgent USB connection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from phone_agent_gateway.mac_client.gateway_client import CallState, PhoneAgentClient

pytestmark = pytest.mark.device_integration


def test_gateway_health_check() -> None:
    client = PhoneAgentClient(host="127.0.0.1", port=8765)
    status = client.get_status()
    assert status.status == "ok"
    assert status.state in (CallState.IDLE, CallState.RINGING, CallState.ACTIVE)
    assert isinstance(status.state_code, int)
    client.close()
