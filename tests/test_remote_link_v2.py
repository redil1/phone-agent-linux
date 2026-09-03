"""Protocol-v2 isolation and protocol-v1 compatibility for the remote link."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections import deque

import pytest

from phone_agent_gateway.ai_bridge.remote_link import (
    PHONE_STREAM_CONNECT_TIMEOUT_SECONDS,
    V2_STREAM_ATTACH_TIMEOUT_SECONDS,
    VERSION,
    VERSION_V2,
    Frame,
    FrameDecoder,
    FrameType,
    RemoteLinkRelay,
    encode_frame,
)
from phone_agent_gateway.mac_client.framed_link import (
    REMOTE_STREAM_HANDSHAKE_TIMEOUT_SECONDS,
)

KEY = b"v2-test-key" * 3


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _FrameReader:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.decoder = FrameDecoder(KEY)
        self.pending: deque[Frame] = deque()

    async def read(self, wait_seconds: float = 2.0) -> Frame:
        while not self.pending:
            chunk = await asyncio.wait_for(self.reader.read(65536), timeout=wait_seconds)
            if not chunk:
                raise EOFError("tunnel closed before the next frame")
            self.pending.extend(self.decoder.feed(chunk))
        return self.pending.popleft()


class _V2Phone:
    def __init__(self) -> None:
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.frames: _FrameReader | None = None
        self.data_writers: list[asyncio.StreamWriter] = []

    async def connect(self, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self.frames = _FrameReader(self.reader)
        self.writer.write(
            encode_frame(FrameType.HELLO, 0, 0, b"phone", KEY, version=VERSION_V2)
        )
        await self.writer.drain()
        ready = await self.frames.read()
        assert (ready.version, ready.type, ready.stream_id, ready.port) == (
            VERSION_V2,
            FrameType.READY,
            0,
            0,
        )

    async def next_open(self) -> Frame:
        assert self.frames is not None
        frame = await self.frames.read()
        assert frame.type == FrameType.OPEN
        assert frame.version == VERSION_V2
        return frame

    async def attach(
        self,
        relay_port: int,
        opened: Frame,
        *,
        challenge: bytes | None = None,
    ) -> tuple[_FrameReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        self.data_writers.append(writer)
        writer.write(
            encode_frame(
                FrameType.HELLO,
                opened.stream_id,
                opened.port,
                opened.payload if challenge is None else challenge,
                KEY,
                version=VERSION_V2,
            )
        )
        await writer.drain()
        frames = _FrameReader(reader)
        return frames, writer

    async def close(self) -> None:
        for writer in self.data_writers:
            writer.close()
        if self.writer is not None:
            self.writer.close()
        for writer in self.data_writers:
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if self.writer is not None:
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()


class _BlockedDrainWriter:
    """A writer whose carrier flow has accepted bytes but cannot drain them."""

    def __init__(self, delegate: asyncio.StreamWriter) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def is_closing(self) -> bool:
        return self.delegate.is_closing()

    def write(self, data: bytes) -> None:
        self.delegate.write(data)

    async def drain(self) -> None:
        self.entered.set()
        await self.release.wait()
        await self.delegate.drain()

    def close(self) -> None:
        self.delegate.close()

    async def wait_closed(self) -> None:
        await self.delegate.wait_closed()


async def _open_v2_stream(
    phone: _V2Phone,
    relay: RemoteLinkRelay,
    presented_port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, Frame, _FrameReader, asyncio.StreamWriter]:
    local_reader, local_writer = await asyncio.open_connection("127.0.0.1", presented_port)
    opened = await phone.next_open()
    assert opened.port == presented_port
    assert len(opened.payload) == 32
    tunnel_frames, tunnel_writer = await phone.attach(relay._listen_port, opened)
    ready = await tunnel_frames.read()
    assert (ready.type, ready.stream_id, ready.port, ready.payload) == (
        FrameType.READY,
        opened.stream_id,
        opened.port,
        b"",
    )
    return local_reader, local_writer, opened, tunnel_frames, tunnel_writer


def test_v2_frames_are_authenticated_without_changing_the_v1_default() -> None:
    v1 = FrameDecoder(KEY).feed(encode_frame(FrameType.DATA, 1, 8766, b"old", KEY))[0]
    v2 = FrameDecoder(KEY).feed(
        encode_frame(FrameType.DATA, 2, 8767, b"new", KEY, version=VERSION_V2)
    )[0]

    assert VERSION == 1
    assert (v1.version, v1.payload) == (1, b"old")
    assert (v2.version, v2.payload) == (2, b"new")


def test_relay_attach_budget_exceeds_the_phone_connect_budget() -> None:
    assert PHONE_STREAM_CONNECT_TIMEOUT_SECONDS == 15.0
    assert (
        PHONE_STREAM_CONNECT_TIMEOUT_SECONDS
        < V2_STREAM_ATTACH_TIMEOUT_SECONDS
        < REMOTE_STREAM_HANDSHAKE_TIMEOUT_SECONDS
    )


@pytest.mark.asyncio
async def test_v2_accepts_a_data_tunnel_that_arrives_within_its_attach_budget() -> None:
    presented = _free_port()
    relay = RemoteLinkRelay(
        KEY,
        listen_host="127.0.0.1",
        listen_port=_free_port(),
        ports=(presented,),
        ping_interval=60,
        stream_attach_timeout=0.25,
    )
    await relay.start()
    phone = _V2Phone()
    await phone.connect(relay._listen_port)

    try:
        local_reader, local_writer = await asyncio.open_connection("127.0.0.1", presented)
        opened = await phone.next_open()
        await asyncio.sleep(0.10)
        tunnel_frames, tunnel_writer = await phone.attach(relay._listen_port, opened)
        ready = await tunnel_frames.read()
        assert (ready.type, ready.stream_id, ready.port) == (
            FrameType.READY,
            opened.stream_id,
            opened.port,
        )

        tunnel_writer.write(
            encode_frame(
                FrameType.DATA,
                opened.stream_id,
                opened.port,
                b"late-but-valid",
                KEY,
                version=VERSION_V2,
            )
        )
        await tunnel_writer.drain()
        assert await asyncio.wait_for(local_reader.readexactly(14), timeout=1) == b"late-but-valid"
        assert relay.stats.last_stream_attach_ms >= 80
        assert relay.stats.stream_attach_timeouts == 0
        assert relay.stats.last_error == ""
        local_writer.close()
    finally:
        await phone.close()
        await relay.close()


@pytest.mark.asyncio
async def test_v2_uses_a_coordinator_and_one_authenticated_tunnel_per_stream() -> None:
    presented = _free_port()
    relay = RemoteLinkRelay(
        KEY,
        listen_host="127.0.0.1",
        listen_port=_free_port(),
        ports=(presented,),
        ping_interval=60,
    )
    await relay.start()
    phone = _V2Phone()
    await phone.connect(relay._listen_port)

    try:
        local_reader, local_writer, opened, tunnel_frames, tunnel_writer = (
            await _open_v2_stream(phone, relay, presented)
        )

        local_writer.write(b"request")
        await local_writer.drain()
        to_phone = await tunnel_frames.read()
        assert (to_phone.type, to_phone.stream_id, to_phone.port, to_phone.payload) == (
            FrameType.DATA,
            opened.stream_id,
            presented,
            b"request",
        )

        tunnel_writer.write(
            encode_frame(
                FrameType.DATA,
                opened.stream_id,
                presented,
                b"response",
                KEY,
                version=VERSION_V2,
            )
        )
        await tunnel_writer.drain()
        assert await asyncio.wait_for(local_reader.readexactly(8), timeout=2) == b"response"
        assert relay.stats.protocol_version == VERSION_V2
        assert relay.stats.streams_open == 1
    finally:
        await phone.close()
        await relay.close()


@pytest.mark.asyncio
async def test_v2_wrong_challenge_cannot_bind_or_drop_the_coordinator() -> None:
    presented = _free_port()
    relay = RemoteLinkRelay(
        KEY,
        listen_host="127.0.0.1",
        listen_port=_free_port(),
        ports=(presented,),
        ping_interval=60,
    )
    await relay.start()
    phone = _V2Phone()
    await phone.connect(relay._listen_port)
    _local_reader, local_writer = await asyncio.open_connection("127.0.0.1", presented)
    opened = await phone.next_open()

    try:
        bad_reader, bad_writer = await phone.attach(
            relay._listen_port, opened, challenge=b"x" * len(opened.payload)
        )
        with pytest.raises(EOFError):
            await bad_reader.read()

        assert relay.stats.phone_connected is True
        assert relay.stats.protocol_version == VERSION_V2
        assert relay._streams[opened.stream_id].tunnel_writer is None

        good_frames, _ = await phone.attach(relay._listen_port, opened)
        ready = await good_frames.read()
        assert (ready.type, ready.stream_id, ready.port) == (
            FrameType.READY,
            opened.stream_id,
            opened.port,
        )
    finally:
        local_writer.close()
        await phone.close()
        await relay.close()


@pytest.mark.asyncio
async def test_v2_rejects_cross_stream_frames_without_dropping_other_streams() -> None:
    ports = (_free_port(), _free_port())
    relay = RemoteLinkRelay(
        KEY,
        listen_host="127.0.0.1",
        listen_port=_free_port(),
        ports=ports,
        ping_interval=60,
    )
    await relay.start()
    phone = _V2Phone()
    await phone.connect(relay._listen_port)

    try:
        first = await _open_v2_stream(phone, relay, ports[0])
        second = await _open_v2_stream(phone, relay, ports[1])
        _, first_local, first_open, _, first_tunnel = first
        second_local_reader, _, second_open, _, second_tunnel = second

        first_tunnel.write(
            encode_frame(
                FrameType.DATA,
                second_open.stream_id,
                second_open.port,
                b"cross-bind",
                KEY,
                version=VERSION_V2,
            )
        )
        await first_tunnel.drain()
        await asyncio.sleep(0.1)
        assert first_open.stream_id not in relay._streams
        assert second_open.stream_id in relay._streams
        assert relay.stats.phone_connected is True

        second_tunnel.write(
            encode_frame(
                FrameType.DATA,
                second_open.stream_id,
                second_open.port,
                b"still-live",
                KEY,
                version=VERSION_V2,
            )
        )
        await second_tunnel.drain()
        assert await asyncio.wait_for(second_local_reader.readexactly(10), timeout=2) == b"still-live"
        first_local.close()
    finally:
        await phone.close()
        await relay.close()


@pytest.mark.asyncio
async def test_blocked_capture_carrier_does_not_block_ack_or_control_stream() -> None:
    """The production failure v2 fixes: media cannot starve playout ACKs."""

    capture_port, control_port = _free_port(), _free_port()
    relay = RemoteLinkRelay(
        KEY,
        listen_host="127.0.0.1",
        listen_port=_free_port(),
        ports=(capture_port, control_port),
        ping_interval=60,
    )
    await relay.start()
    phone = _V2Phone()
    await phone.connect(relay._listen_port)

    try:
        capture = await _open_v2_stream(phone, relay, capture_port)
        control = await _open_v2_stream(phone, relay, control_port)
        _, capture_local, capture_open, _, _ = capture
        control_local_reader, control_local, control_open, control_frames, control_tunnel = control
        assert capture_open.payload != control_open.payload

        capture_stream = relay._streams[capture_open.stream_id]
        assert capture_stream.tunnel_writer is not None
        blocked = _BlockedDrainWriter(capture_stream.tunnel_writer)
        capture_stream.tunnel_writer = blocked  # type: ignore[assignment]

        capture_local.write(b"caller-audio")
        await capture_local.drain()
        await asyncio.wait_for(blocked.entered.wait(), timeout=2)

        # Runtime -> phone control still crosses its own carrier while the
        # capture carrier's drain remains deliberately blocked.
        control_local.write(b"flush-generation")
        await control_local.drain()
        command = await control_frames.read(wait_seconds=0.5)
        assert command.payload == b"flush-generation"

        # Phone -> runtime playout acknowledgement also progresses on that
        # independent stream, which prevents false six-second ACK timeouts.
        control_tunnel.write(
            encode_frame(
                FrameType.DATA,
                control_open.stream_id,
                control_open.port,
                b"rendered:42",
                KEY,
                version=VERSION_V2,
            )
        )
        await control_tunnel.drain()
        assert await asyncio.wait_for(control_local_reader.readexactly(11), timeout=0.5) == (
            b"rendered:42"
        )
        assert blocked.release.is_set() is False
        blocked.release.set()
    finally:
        for stream in relay._streams.values():
            tunnel = stream.tunnel_writer
            if isinstance(tunnel, _BlockedDrainWriter):
                tunnel.release.set()
        await phone.close()
        await relay.close()


@pytest.mark.asyncio
async def test_v1_phone_still_multiplexes_data_on_its_original_tunnel() -> None:
    """An installed v1 APK keeps the exact original single-tunnel behavior."""

    presented = _free_port()
    relay = RemoteLinkRelay(
        KEY,
        listen_host="127.0.0.1",
        listen_port=_free_port(),
        ports=(presented,),
        ping_interval=60,
    )
    await relay.start()
    phone_reader, phone_writer = await asyncio.open_connection(
        "127.0.0.1", relay._listen_port
    )
    frames = _FrameReader(phone_reader)
    phone_writer.write(encode_frame(FrameType.HELLO, 0, 0, b"phone", KEY))
    await phone_writer.drain()
    assert (await frames.read()).type == FrameType.READY

    try:
        local_reader, local_writer = await asyncio.open_connection("127.0.0.1", presented)
        opened = await frames.read()
        assert (opened.version, opened.type, opened.payload) == (VERSION, FrameType.OPEN, b"")

        local_writer.write(b"legacy-out")
        await local_writer.drain()
        outbound = await frames.read()
        assert (outbound.version, outbound.stream_id, outbound.payload) == (
            VERSION,
            opened.stream_id,
            b"legacy-out",
        )

        phone_writer.write(
            encode_frame(FrameType.DATA, opened.stream_id, presented, b"legacy-in", KEY)
        )
        await phone_writer.drain()
        assert await asyncio.wait_for(local_reader.readexactly(9), timeout=2) == b"legacy-in"
        assert relay.stats.protocol_version == VERSION
        local_writer.close()
    finally:
        phone_writer.close()
        with contextlib.suppress(Exception):
            await phone_writer.wait_closed()
        await relay.close()
