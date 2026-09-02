"""Offline integration tests for the authenticated three-channel phone link."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import Counter, deque
from queue import Empty, Queue
from typing import Any
from uuid import UUID, uuid4

import pytest

from phone_agent_gateway.ai_bridge.media_protocol import (
    FrameDirection,
    FrameFlags,
    FrameKind,
    FrameStreamDecoder,
    MediaFrame,
    encode_frame,
)
from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase
from phone_agent_gateway.mac_client.framed_link import FramedGatewayLink, LinkPorts
from phone_agent_gateway.mac_client.gateway_client import CallState
from phone_agent_gateway.mac_client.protocol_client import AuthenticatedPhoneAgentClient

KEY = bytes(range(32))


def test_adb_forward_can_map_alternate_local_control_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def record(command: list[str], **_kwargs: Any) -> None:
        commands.append(command)

    monkeypatch.setattr(
        "phone_agent_gateway.mac_client.framed_link.subprocess.run",
        record,
    )
    link = FramedGatewayLink(
        CallSessionState(),
        KEY,
        ports=LinkPorts(control=18768),
        remote_ports=LinkPorts(control=8768),
        device_id="phone-id",
    )

    link.ensure_adb_forward()

    assert commands[-1] == [
        "adb",
        "-s",
        "phone-id",
        "forward",
        "tcp:18768",
        "tcp:8768",
    ]


def test_remote_relay_fallback_never_probes_framed_media_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    probes: list[tuple[str, int]] = []

    def unavailable(command: list[str], **_kwargs: Any) -> None:
        commands.append(command)
        raise FileNotFoundError("adb")

    def record_probe(host: str, port: int, timeout: float = 0.5) -> bool:
        del timeout
        probes.append((host, port))
        return True

    monkeypatch.setattr(
        "phone_agent_gateway.mac_client.framed_link.subprocess.run", unavailable
    )
    monkeypatch.setattr(
        "phone_agent_gateway.mac_client.framed_link._port_is_open", record_probe
    )
    ports = LinkPorts(legacy_http=18765, downlink=18766, uplink=18767, control=18768)
    link = FramedGatewayLink(CallSessionState(), KEY, ports=ports)

    link.ensure_adb_forward()

    assert probes == [("127.0.0.1", 18765)]
    assert len(commands) == 1


def test_control_request_discards_a_stale_late_acknowledgement() -> None:
    gateway = FakeFramedGateway()
    gateway.start()
    session = active_session()
    link = FramedGatewayLink(
        session,
        KEY,
        ports=gateway.ports,
        auto_forward_adb=False,
    )
    try:
        link.connect_control()
        stale_id = uuid4()
        gateway.stale_control_acks.put(str(stale_id))
        result = link.request("call.status")
        assert result["state"] == "ACTIVE"
    finally:
        link.close()
        gateway.close()


class FakeFramedGateway:
    def __init__(self) -> None:
        self.listeners = {name: self._listener() for name in ("control", "uplink", "downlink")}
        self.ports = LinkPorts(
            legacy_http=1,
            control=self.listeners["control"].getsockname()[1],
            uplink=self.listeners["uplink"].getsockname()[1],
            downlink=self.listeners["downlink"].getsockname()[1],
        )
        self.stop = threading.Event()
        self.handshakes: Queue[tuple[str, dict[str, Any]]] = Queue()
        self.downlink: Queue[bytes] = Queue()
        self.uplink: Queue[MediaFrame] = Queue()
        self.auto_ack_uplink = True
        self.deferred_uplink_acks: Queue[tuple[socket.socket, MediaFrame]] = Queue()
        self.command_executions: Counter[str] = Counter()
        self.commands: Queue[dict[str, Any]] = Queue()
        self.stale_control_acks: Queue[str] = Queue()
        self._responses: dict[str, dict[str, Any]] = {}
        self._threads = [
            threading.Thread(target=self._serve, args=(name,), daemon=True)
            for name in self.listeners
        ]

    @staticmethod
    def _listener() -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.2)
        return listener

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        self.stop.set()
        for listener in self.listeners.values():
            listener.close()
        for thread in self._threads:
            thread.join(timeout=1)

    def _serve(self, name: str) -> None:
        listener = self.listeners[name]
        while not self.stop.is_set():
            try:
                client, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._serve_client,
                args=(name, client),
                daemon=True,
            ).start()

    def _serve_client(self, name: str, client: socket.socket) -> None:
        try:
            self._serve_connection(name, client)
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _serve_connection(self, name: str, client: socket.socket) -> None:
        client.settimeout(0.2)
        decoder = FrameStreamDecoder(authentication_key=KEY)
        pending: deque[MediaFrame] = deque()
        hello = self._receive(client, decoder, pending)
        body = hello.json_payload()
        assert body["type"] == "gateway.hello"
        assert body["channel"] == name
        self.handshakes.put((name, body))
        self._send_json(
            client,
            hello,
            FrameKind.ACK,
            {
                "type": "gateway.ready",
                "status": "ok",
                "link_epoch": body["link_epoch"],
                "generation": hello.generation_id,
            },
        )

        if name == "control":
            self._serve_control(client, decoder, pending)
        elif name == "uplink":
            self._serve_uplink(client, decoder, pending)
        else:
            self._serve_downlink(client, hello)

    def _serve_control(
        self,
        client: socket.socket,
        decoder: FrameStreamDecoder,
        pending: deque[MediaFrame],
    ) -> None:
        while not self.stop.is_set():
            try:
                frame = self._receive(client, decoder, pending)
            except TimeoutError:
                continue
            except (EOFError, OSError):
                return
            body = frame.json_payload()
            command_id = body["command_id"]
            try:
                stale_id = self.stale_control_acks.get_nowait()
            except Empty:
                pass
            else:
                self._send_json(
                    client,
                    frame,
                    FrameKind.ACK,
                    {
                        "type": "command.ack",
                        "status": "ok",
                        "command_id": stale_id,
                    },
                )
            response = self._responses.get(command_id)
            if response is None:
                command_type = body["type"]
                self.command_executions[command_type] += 1
                self.commands.put(body)
                response = {
                    "type": "command.ack",
                    "status": "ok",
                    "command_id": command_id,
                    "generation": body["payload"].get(
                        "next_generation", frame.generation_id
                    ),
                }
                if command_type == "call.status":
                    response.update(
                        {"state": "ACTIVE", "state_code": 4, "incoming_number": ""}
                    )
                self._responses[command_id] = response
            self._send_json(client, frame, FrameKind.ACK, response)

    def _serve_uplink(
        self,
        client: socket.socket,
        decoder: FrameStreamDecoder,
        pending: deque[MediaFrame],
    ) -> None:
        while not self.stop.is_set():
            try:
                frame = self._receive(client, decoder, pending)
                self.uplink.put(frame)
                if self.auto_ack_uplink:
                    self._send_playout_ack(client, frame)
                else:
                    self.deferred_uplink_acks.put((client, frame))
            except TimeoutError:
                continue
            except (EOFError, OSError):
                return

    def acknowledge_one_uplink(self) -> None:
        client, frame = self.deferred_uplink_acks.get(timeout=1)
        self._send_playout_ack(client, frame)

    def _send_playout_ack(self, client: socket.socket, frame: MediaFrame) -> None:
        self._send_json(
            client,
            frame,
            FrameKind.ACK,
            {
                "type": "audio.playout.ack",
                "status": "ok",
                "generation": frame.generation_id,
                "sequence": frame.sequence,
            },
        )

    def _serve_downlink(self, client: socket.socket, hello: MediaFrame) -> None:
        while not self.stop.is_set():
            try:
                payload = self.downlink.get(timeout=0.2)
            except Empty:
                continue
            frame = MediaFrame(
                kind=FrameKind.AUDIO,
                direction=FrameDirection.PHONE_TO_MAC,
                call_id=hello.call_id,
                generation_id=hello.generation_id,
                sequence=0,
                monotonic_ns=time.monotonic_ns(),
                payload=payload,
                sample_rate=16_000,
                channels=1,
                sample_width=2,
            )
            client.sendall(encode_frame(frame, authentication_key=KEY))

    @staticmethod
    def _receive(
        client: socket.socket,
        decoder: FrameStreamDecoder,
        pending: deque[MediaFrame],
    ) -> MediaFrame:
        if pending:
            return pending.popleft()
        while True:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise EOFError
            frames = decoder.feed(chunk)
            if frames:
                pending.extend(frames[1:])
                return frames[0]

    @staticmethod
    def _send_json(
        client: socket.socket,
        request: MediaFrame,
        kind: FrameKind,
        body: dict[str, Any],
    ) -> None:
        response = MediaFrame(
            kind=kind,
            direction=FrameDirection.PHONE_TO_MAC,
            call_id=request.call_id,
            generation_id=request.generation_id,
            sequence=request.sequence,
            monotonic_ns=time.monotonic_ns(),
            payload=json.dumps(body, separators=(",", ":"), sort_keys=True).encode(),
        )
        client.sendall(encode_frame(response, authentication_key=KEY))


def active_session() -> CallSessionState:
    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    session.set_phase(SessionPhase.ACTIVE)
    return session


def test_authenticated_link_routes_media_and_idempotent_control() -> None:
    gateway = FakeFramedGateway()
    gateway.start()
    session = active_session()
    received: Queue[MediaFrame] = Queue()
    link = FramedGatewayLink(
        session,
        KEY,
        ports=gateway.ports,
        auto_forward_adb=False,
    )
    link.on_audio_received(received.put)
    try:
        link.connect()
        gateway.downlink.put(b"\x01\x00" * 320)
        assert received.get(timeout=1).payload == b"\x01\x00" * 320

        generation, sequence = session.next_output_identity()
        link.send_audio_chunk(b"\x02\x00" * 320, generation, sequence)
        assert gateway.uplink.get(timeout=1).payload == b"\x02\x00" * 320

        marker_generation, marker_sequence = session.next_output_identity()
        link.send_audio_end_marker(marker_generation, marker_sequence)
        marker = gateway.uplink.get(timeout=1)
        assert marker.kind is FrameKind.CONTROL
        assert marker.flags & FrameFlags.END_OF_STREAM
        assert marker.payload == b""

        command_id = uuid4()
        first = link.request("gateway.health", command_id=command_id)
        second = link.request("gateway.health", command_id=command_id)
        assert first == second
        assert gateway.command_executions["gateway.health"] == 1
    finally:
        link.close()
        gateway.close()


def test_reconnect_uses_new_epoch_without_reducing_generation() -> None:
    gateway = FakeFramedGateway()
    gateway.start()
    session = active_session()
    link = FramedGatewayLink(
        session,
        KEY,
        ports=gateway.ports,
        auto_forward_adb=False,
    )
    try:
        link.connect()
        first_epoch = session.link_epoch
        session.interrupt("test")
        generation = session.generation_id
        link.reconnect()
        assert session.link_epoch != first_epoch
        assert session.generation_id == generation

        epochs: set[UUID] = set()
        while not gateway.handshakes.empty():
            _name, body = gateway.handshakes.get_nowait()
            epochs.add(UUID(body["link_epoch"]))
        assert first_epoch in epochs
        assert session.link_epoch in epochs
    finally:
        link.close()
        gateway.close()


def test_uplink_never_exceeds_phone_playout_window_without_render_acks() -> None:
    gateway = FakeFramedGateway()
    gateway.auto_ack_uplink = False
    gateway.start()
    session = active_session()
    link = FramedGatewayLink(
        session,
        KEY,
        ports=gateway.ports,
        auto_forward_adb=False,
    )
    sent = threading.Event()

    def send_beyond_window() -> None:
        for _ in range(link.UPLINK_WINDOW_FRAMES + 1):
            generation, sequence = session.next_output_identity()
            link.send_audio_chunk(b"\x03\x00" * 320, generation, sequence)
        sent.set()

    try:
        link.connect()
        sender = threading.Thread(target=send_beyond_window, daemon=True)
        sender.start()

        deadline = time.monotonic() + 1
        while gateway.uplink.qsize() < link.UPLINK_WINDOW_FRAMES:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.05)
        assert gateway.uplink.qsize() == link.UPLINK_WINDOW_FRAMES
        assert not sent.is_set()

        gateway.acknowledge_one_uplink()
        assert sent.wait(1)
        assert gateway.uplink.get(timeout=1)
    finally:
        link.close()
        gateway.close()


def test_authenticated_client_uses_framed_control_and_normalizes_number() -> None:
    gateway = FakeFramedGateway()
    gateway.start()
    session = active_session()
    client = AuthenticatedPhoneAgentClient(
        session,
        KEY,
        ports=gateway.ports,
        auto_forward_adb=False,
    )
    try:
        client.connect_control()
        client.dial("+212 6-12-34-56-78")
        status = client.get_status()
        commands = [gateway.commands.get(timeout=1), gateway.commands.get(timeout=1)]
        assert commands[0]["type"] == "call.dial"
        assert commands[0]["payload"]["number"] == "0612345678"
        assert status.state is CallState.ACTIVE
    finally:
        client.close()
        gateway.close()
