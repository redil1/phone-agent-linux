"""Offline HTTP contract tests for telephony control operations.

These tests deliberately use an ephemeral loopback server. They must never depend on,
or mutate, a connected phone merely because a developer selects the wrong pytest marker.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from phone_agent_gateway.mac_client.gateway_client import PhoneAgentClient


class _RecordingGateway(ThreadingHTTPServer):
    requests: list[tuple[str, dict[str, str], dict[str, str]]]


class _Handler(BaseHTTPRequestHandler):
    server: _RecordingGateway

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        self._reply()

    def do_POST(self) -> None:
        self._reply()

    def _reply(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b""
        body = json.loads(raw_body) if raw_body else {}
        self.server.requests.append((parsed.path, query, body))

        if parsed.path == "/call/status":
            response = {
                "status": "ok",
                "state": "IDLE",
                "state_code": 0,
                "incoming_number": "",
            }
        elif parsed.path == "/call/dtmf":
            response = {"status": "ok", "action": "dtmf_sent", "digit": body["digit"]}
        elif parsed.path == "/call/hangup":
            response = {"status": "ok", "action": "hung_up"}
        else:
            response = {"status": "error", "message": "unexpected endpoint"}

        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def gateway() -> Iterator[tuple[_RecordingGateway, PhoneAgentClient]]:
    server = _RecordingGateway(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = PhoneAgentClient(
        host="127.0.0.1",
        port=server.server_port,
        auto_forward_adb=False,
    )
    try:
        yield server, client
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dtmf_digit_dispatch(gateway: tuple[_RecordingGateway, PhoneAgentClient]) -> None:
    server, client = gateway
    response = client.send_dtmf("5")

    assert response == {"status": "ok", "action": "dtmf_sent", "digit": "5"}
    assert server.requests == [("/call/dtmf", {"digit": "5"}, {"digit": "5"})]


def test_hangup_command(gateway: tuple[_RecordingGateway, PhoneAgentClient]) -> None:
    server, client = gateway
    response = client.hangup()

    assert response == {"status": "ok", "action": "hung_up"}
    assert server.requests == [("/call/hangup", {}, {})]


def test_call_state_listener(gateway: tuple[_RecordingGateway, PhoneAgentClient]) -> None:
    server, client = gateway
    received = []
    delivered = threading.Event()

    def callback(status: object) -> None:
        received.append(status)
        delivered.set()

    client.add_call_listener(callback)

    assert delivered.wait(timeout=2)
    assert received[0].status == "ok"
    assert server.requests[0] == ("/call/status", {}, {})
