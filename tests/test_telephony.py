"""Tests for PhoneAgent Telephony control operations."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from phone_agent_gateway.mac_client.gateway_client import PhoneAgentClient

pytestmark = pytest.mark.device_integration


def test_dtmf_digit_dispatch() -> None:
    client = PhoneAgentClient(host="127.0.0.1", port=8765)
    res = client.send_dtmf("5")
    assert res.get("status") == "ok"
    assert res.get("action") == "dtmf_sent"
    assert res.get("digit") == "5"
    client.close()


def test_hangup_command() -> None:
    client = PhoneAgentClient(host="127.0.0.1", port=8765)
    res = client.hangup()
    assert res.get("status") == "ok"
    assert res.get("action") == "hung_up"
    client.close()


def test_call_state_listener() -> None:
    client = PhoneAgentClient(host="127.0.0.1", port=8765)
    received = []

    def callback(status):
        received.append(status)

    client.add_call_listener(callback)
    time.sleep(1.2)
    assert len(received) >= 1
    assert received[0].status == "ok"
    client.close()
