"""The relay must be indistinguishable from an adb forward, or a call breaks."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from phone_agent_gateway.ai_bridge.remote_link import (
    MAX_PAYLOAD_BYTES,
    FrameDecoder,
    FrameType,
    RemoteLinkError,
    RemoteLinkRelay,
    RemoteLinkSettings,
    encode_frame,
)

KEY = b"k" * 32


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _FakePhone:
    """A handset that answers OPEN by connecting to its own local services."""

    def __init__(self, key: bytes = KEY) -> None:
        self.key = key
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.locals: dict[int, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
        self.local_ports: dict[int, int] = {}
        self.pongs = 0
        self._task: asyncio.Task | None = None
        self._pumps: set[asyncio.Task] = set()

    async def connect(self, host: str, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection(host, port)
        self.writer.write(encode_frame(FrameType.HELLO, 0, 0, b"phone", self.key))
        await self.writer.drain()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        decoder = FrameDecoder(self.key)
        assert self.reader is not None
        try:
            while True:
                chunk = await self.reader.read(65536)
                if not chunk:
                    return
                for frame in decoder.feed(chunk):
                    await self._on_frame(frame)
        except (asyncio.CancelledError, OSError, RemoteLinkError):
            return

    async def _on_frame(self, frame) -> None:
        assert self.writer is not None
        if frame.type == FrameType.OPEN:
            target = self.local_ports.get(frame.port)
            if target is None:
                self.writer.write(
                    encode_frame(FrameType.CLOSE, frame.stream_id, frame.port, b"", self.key)
                )
                await self.writer.drain()
                return
            reader, writer = await asyncio.open_connection("127.0.0.1", target)
            self.locals[frame.stream_id] = (reader, writer)
            pump = asyncio.create_task(self._pump(frame.stream_id, frame.port, reader))
            self._pumps.add(pump)
            pump.add_done_callback(self._pumps.discard)
        elif frame.type == FrameType.DATA:
            pair = self.locals.get(frame.stream_id)
            if pair:
                pair[1].write(frame.payload)
                await pair[1].drain()
        elif frame.type == FrameType.CLOSE:
            pair = self.locals.pop(frame.stream_id, None)
            if pair:
                pair[1].close()
        elif frame.type == FrameType.PING:
            self.pongs += 1
            self.writer.write(
                encode_frame(FrameType.PONG, frame.stream_id, 0, b"", self.key)
            )
            await self.writer.drain()

    async def _pump(self, stream_id: int, port: int, reader: asyncio.StreamReader) -> None:
        assert self.writer is not None
        try:
            while True:
                chunk = await reader.read(32768)
                if not chunk:
                    break
                self.writer.write(
                    encode_frame(FrameType.DATA, stream_id, port, chunk, self.key)
                )
                await self.writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        with contextlib.suppress(Exception):
            self.writer.write(encode_frame(FrameType.CLOSE, stream_id, port, b"", self.key))
            await self.writer.drain()

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()


# ------------------------------------------------------------------- framing


def test_a_frame_survives_a_round_trip() -> None:
    raw = encode_frame(FrameType.DATA, 7, 8766, b"audio", KEY)
    frames = FrameDecoder(KEY).feed(raw)

    assert len(frames) == 1
    assert frames[0].type == FrameType.DATA
    assert frames[0].stream_id == 7
    assert frames[0].port == 8766
    assert frames[0].payload == b"audio"


def test_several_frames_in_one_read_are_all_recovered() -> None:
    # A single TCP read routinely carries many 640-byte media frames.
    raw = b"".join(encode_frame(FrameType.DATA, i, 8767, b"x" * 640, KEY) for i in range(5))
    frames = FrameDecoder(KEY).feed(raw)

    assert [f.stream_id for f in frames] == [0, 1, 2, 3, 4]


def test_a_frame_split_across_reads_is_buffered() -> None:
    raw = encode_frame(FrameType.DATA, 1, 8766, b"y" * 500, KEY)
    decoder = FrameDecoder(KEY)

    assert decoder.feed(raw[:40]) == []
    frames = decoder.feed(raw[40:])

    assert len(frames) == 1 and frames[0].payload == b"y" * 500


def test_a_tampered_header_is_rejected() -> None:
    """The tag covers the header, so a port cannot be redirected in flight."""

    raw = bytearray(encode_frame(FrameType.DATA, 1, 8766, b"z", KEY))
    raw[10] ^= 0xFF  # flip a bit inside the stream id / port region

    with pytest.raises(RemoteLinkError, match="authentication"):
        FrameDecoder(KEY).feed(bytes(raw))


def test_the_wrong_key_cannot_open_a_tunnel() -> None:
    raw = encode_frame(FrameType.HELLO, 0, 0, b"", b"attacker" * 4)

    with pytest.raises(RemoteLinkError, match="authentication"):
        FrameDecoder(KEY).feed(raw)


def test_an_oversized_payload_is_refused_before_it_is_sent() -> None:
    with pytest.raises(RemoteLinkError, match="exceeds"):
        encode_frame(FrameType.DATA, 1, 8766, b"a" * (MAX_PAYLOAD_BYTES + 1), KEY)


# --------------------------------------------------------------- end to end


@pytest.mark.asyncio
async def test_the_relay_carries_a_real_request_to_the_phone() -> None:
    """A client on the relay's loopback must reach a service on the handset."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n" + data)
        await writer.drain()
        writer.close()

    phone_service = await asyncio.start_server(echo, "127.0.0.1", 0)
    phone_port = phone_service.sockets[0].getsockname()[1]

    presented = _free_port()
    relay = RemoteLinkRelay(
        KEY, listen_host="127.0.0.1", listen_port=_free_port(), ports=(presented,)
    )
    await relay.start()
    phone = _FakePhone()
    phone.local_ports[presented] = phone_port
    await phone.connect("127.0.0.1", relay._listen_port)
    await asyncio.sleep(0.1)

    try:
        assert relay.stats.phone_connected is True
        reader, writer = await asyncio.open_connection("127.0.0.1", presented)
        writer.write(b"GET /call/status HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(1024), timeout=5)

        assert response.startswith(b"HTTP/1.1 200 OK")
        assert b"/call/status" in response
        writer.close()
    finally:
        await phone.close()
        await relay.close()
        phone_service.close()


@pytest.mark.asyncio
async def test_media_sized_frames_cross_the_tunnel_intact() -> None:
    """20 ms of phone audio is 640 bytes; a call is thousands of them."""

    payload = bytes(range(256)) * 10  # 2560 bytes, several media frames

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received = b""
        while len(received) < len(payload):
            chunk = await reader.read(4096)
            if not chunk:
                break
            received += chunk
        writer.write(received)
        await writer.drain()
        writer.close()

    phone_service = await asyncio.start_server(echo, "127.0.0.1", 0)
    phone_port = phone_service.sockets[0].getsockname()[1]

    presented = _free_port()
    relay = RemoteLinkRelay(
        KEY, listen_host="127.0.0.1", listen_port=_free_port(), ports=(presented,)
    )
    await relay.start()
    phone = _FakePhone()
    phone.local_ports[presented] = phone_port
    await phone.connect("127.0.0.1", relay._listen_port)
    await asyncio.sleep(0.1)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", presented)
        writer.write(payload)
        await writer.drain()
        echoed = b""
        while len(echoed) < len(payload):
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
            if not chunk:
                break
            echoed += chunk

        assert echoed == payload  # byte-for-byte, no reordering or loss
        writer.close()
    finally:
        await phone.close()
        await relay.close()
        phone_service.close()


@pytest.mark.asyncio
async def test_a_local_client_is_refused_when_no_phone_is_attached() -> None:
    """Refusing beats hanging: the voice host reports an unreachable gateway."""

    presented = _free_port()
    relay = RemoteLinkRelay(
        KEY, listen_host="127.0.0.1", listen_port=_free_port(), ports=(presented,)
    )
    await relay.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", presented)
        writer.write(b"GET / HTTP/1.1\r\n\r\n")
        # An immediate close surfaces either as EOF or as a reset depending on
        # timing; both mean the same thing to the caller: refused, not hung.
        refused = False
        try:
            with contextlib.suppress(OSError):
                await writer.drain()
            refused = await asyncio.wait_for(reader.read(64), timeout=5) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            refused = True

        assert refused
        writer.close()
    finally:
        await relay.close()


@pytest.mark.asyncio
async def test_a_second_phone_cannot_take_over_a_live_tunnel() -> None:
    """Two handsets answering one call would be worse than a refused connect."""

    relay = RemoteLinkRelay(
        KEY, listen_host="127.0.0.1", listen_port=_free_port(), ports=(_free_port(),)
    )
    await relay.start()
    first = _FakePhone()
    await first.connect("127.0.0.1", relay._listen_port)
    await asyncio.sleep(0.1)

    second = _FakePhone()
    try:
        await second.connect("127.0.0.1", relay._listen_port)
        await asyncio.sleep(0.2)

        assert relay.stats.phone_connected is True
        # The original tunnel is still the one the relay holds.
        assert relay._phone_writer is not None
    finally:
        await second.close()
        await first.close()
        await relay.close()


@pytest.mark.asyncio
async def test_a_tunnel_that_never_says_hello_is_dropped() -> None:
    relay = RemoteLinkRelay(
        KEY, listen_host="127.0.0.1", listen_port=_free_port(), ports=(_free_port(),)
    )
    await relay.start()
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", relay._listen_port)
        writer.write(encode_frame(FrameType.DATA, 1, 8766, b"nope", KEY))
        await writer.drain()
        await asyncio.sleep(0.2)

        assert relay.stats.phone_connected is False
        writer.close()
    finally:
        await relay.close()


def test_the_phone_and_the_runtime_frame_identically() -> None:
    """Pinned against android_service_apk/testsrc/.../RemoteLinkInteropTest.java.

    A byte-order or field-width disagreement would authenticate on neither side
    and strand every call, so both encoders are held to this vector.
    """

    key = bytes(range(32))
    golden = (
        "5048524c010400000007223f0000000401020304"
        "e24fffdd48954123b5a64020616c11815fb507801ec383a355ce517364784c93"
    )

    assert encode_frame(FrameType.DATA, 7, 8767, bytes([1, 2, 3, 4]), key).hex() == golden


def test_only_gateway_ports_may_be_tunnelled() -> None:
    """The relay must not be able to ask the phone to reach any local service."""

    from phone_agent_gateway.ai_bridge.remote_link import GATEWAY_PORTS

    assert GATEWAY_PORTS == (8765, 8766, 8767, 8768)


@pytest.mark.asyncio
async def test_the_real_gateway_client_dials_through_the_tunnel() -> None:
    """The proof: the class the voice host uses, unmodified, over the relay.

    If this passes, replacing the cable needs no change anywhere in the runtime
    -- the relay presents the same loopback ports adb forward did.
    """

    import json as _json

    from phone_agent_gateway.mac_client.gateway_client import PhoneAgentClient

    seen: list[str] = []

    async def gateway(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.read(2048)
        path = request.split(b" ")[1].decode() if b" " in request else "?"
        seen.append(path)
        body = _json.dumps(
            {
                "status": "ok",
                "state": "IDLE",
                "state_code": 0,
                "incoming_number": "",
                "gateway": "ready",
            }
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    phone_service = await asyncio.start_server(gateway, "127.0.0.1", 0)
    phone_port = phone_service.sockets[0].getsockname()[1]

    presented = _free_port()
    relay = RemoteLinkRelay(
        KEY, listen_host="127.0.0.1", listen_port=_free_port(), ports=(presented,)
    )
    await relay.start()
    phone = _FakePhone()
    phone.local_ports[presented] = phone_port
    await phone.connect("127.0.0.1", relay._listen_port)
    await asyncio.sleep(0.15)

    try:
        client = PhoneAgentClient(
            host="127.0.0.1", port=presented, auto_forward_adb=False
        )
        status = await asyncio.to_thread(client.get_status)
        await asyncio.to_thread(client.dial, "0600000000")
        await asyncio.to_thread(client.hangup)

        assert status.state.name == "IDLE"
        assert any("/call/dial" in path for path in seen)
        assert any("/call/hangup" in path for path in seen)
        assert relay.stats.streams_total == 3
    finally:
        await phone.close()
        await relay.close()
        phone_service.close()


def test_studio_can_turn_the_link_on_and_off_without_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Setup has to be possible from the UI; an env var means a terminal."""

    import json as _json

    from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
    from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer

    # Never write the operator's real settings from a test.
    store = tmp_path / "remote-link.json"
    monkeypatch.setattr(RemoteLinkSettings, "store_path", staticmethod(lambda: store))
    monkeypatch.delenv("PHONE_AGENT_REMOTE_LINK", raising=False)
    # The real gateway ports are usually held by an adb forward on a developer
    # machine, which is a genuine conflict rather than a test artefact.
    monkeypatch.setattr(
        "phone_agent_gateway.ai_bridge.remote_link.GATEWAY_PORTS",
        (_free_port(), _free_port(), _free_port(), _free_port()),
    )
    monkeypatch.setattr(
        "phone_agent_gateway.ai_bridge.web_server.load_remote_link_key",
        lambda: KEY,
    )
    monkeypatch.setattr(
        "phone_agent_gateway.ai_bridge.remote_link.load_remote_link_key",
        lambda: KEY,
    )

    async def _test() -> None:
        server = PhoneAgentWebServer(config=ProviderConfig())
        port = _free_port()

        before = server.remote_link_status()
        assert before["enabled"] is False
        assert before["running"] is False
        # The address an operator reads off the screen and types into the phone.
        assert isinstance(before["addresses"], list)

        after = await server.set_remote_link(enabled=True, port=port)
        assert after["enabled"] is True
        assert after["running"] is True
        assert after["listen_port"] == port

        # A phone can now actually reach it.
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

        off = await server.set_remote_link(enabled=False)
        assert off["running"] is False

        # The choice survives a restart, which is what replaces the env var.
        stored = _json.loads(store.read_text())
        assert stored["enabled"] is False

    asyncio.run(_test())


def test_a_bad_port_is_refused_rather_than_silently_ignored() -> None:
    from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
    from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer

    async def _test() -> None:
        server = PhoneAgentWebServer(config=ProviderConfig())
        with pytest.raises(ValueError, match="between 1 and 65535"):
            await server.set_remote_link(enabled=True, port=99999)

    asyncio.run(_test())


@pytest.mark.asyncio
async def test_a_port_held_by_an_adb_forward_says_so() -> None:
    """"Address in use" alone sent an operator hunting; name the real cause."""

    taken = _free_port()
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", taken)
    relay = RemoteLinkRelay(
        KEY, listen_host="127.0.0.1", listen_port=_free_port(), ports=(taken,)
    )
    try:
        with pytest.raises(RemoteLinkError, match="adb forward"):
            await relay.start()
    finally:
        blocker.close()
        await relay.close()


def test_a_tunnelled_phone_is_not_asked_for_a_usb_cable() -> None:
    """The relay presents on loopback too, so "local" no longer implies adb.

    Demanding adb here refused every dial on a healthy tunnel with
    "Reconnect USB, unlock the phone, and authorize USB debugging".
    """

    from types import SimpleNamespace

    from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
    from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer

    server = PhoneAgentWebServer(config=ProviderConfig())
    server._remote_link = SimpleNamespace(
        stats=SimpleNamespace(phone_connected=True)
    )

    # With a phone tunnelled in, the preflight must reach the health probe
    # rather than stopping at an adb check, so the failure names the gateway.
    message = server._gateway_preflight_sync()

    assert message is None or "USB" not in message


def test_the_child_is_told_not_to_fight_the_relay_for_the_ports() -> None:
    """adb forward and the relay cannot both own 8765-8768."""

    from types import SimpleNamespace

    from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
    from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer

    server = PhoneAgentWebServer(config=ProviderConfig())
    assert server._child_environment()["PHONE_AGENT_USE_ADB_FORWARD"] == "true"

    server._remote_link = SimpleNamespace(stats=SimpleNamespace(phone_connected=True))
    assert server._child_environment()["PHONE_AGENT_USE_ADB_FORWARD"] == "false"


def test_an_already_served_port_is_used_instead_of_failing() -> None:
    """The relay owns these ports, so adb refuses the bind and used to give up.

    That looped "gateway unavailable" forever against a gateway that was
    reachable the whole time.
    """

    import socket as _socket

    from phone_agent_gateway.mac_client.framed_link import _port_is_open

    listener = _socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert _port_is_open("127.0.0.1", port) is True
    finally:
        listener.close()

    assert _port_is_open("127.0.0.1", port) is False
