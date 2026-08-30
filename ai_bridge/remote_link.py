"""Carry the four phone gateway ports over one outbound connection.

The Mac reaches the handset today through ``adb forward``, which needs a USB
cable and a machine standing next to the phone. Every call operation is already
plain TCP -- dial, hangup and status are HTTP on 8765, and the media path is
three sockets on 8766-8768 -- so the cable is only a transport, not a
capability. Replacing it lets the runtime live anywhere.

A handset on mobile data usually sits behind carrier NAT, so a server cannot
open a connection to it. The phone therefore dials out, and this relay
multiplexes the four ports back down that single socket. The relay then
re-presents them on its own loopback, which is exactly the shape ``adb forward``
produced, so the voice host needs no change at all: it still talks to
127.0.0.1:8765-8768 and cannot tell the difference.

The phone keeps binding its gateway ports to loopback only. The tunnel client
runs inside the phone process and connects to them locally, so nothing on the
handset is ever exposed to the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("PhoneAgentRemoteLink")

MAGIC = b"PHRL"
VERSION = 1
AUTH_TAG_BYTES = hashlib.sha256().digest_size
# One media frame is 640 bytes; this bounds a hostile or broken peer without
# ever truncating legitimate traffic.
MAX_PAYLOAD_BYTES = 64 * 1024
HEADER = struct.Struct("!4sBBIHI")  # magic, version, type, stream, port, length

# The gateway ports, in the order the relay presents them.
GATEWAY_PORTS: tuple[int, ...] = (8765, 8766, 8767, 8768)


class FrameType:
    HELLO = 1  # phone -> relay, authenticates the tunnel
    READY = 2  # relay -> phone, tunnel accepted
    OPEN = 3  # relay -> phone, open a local connection for this stream
    DATA = 4  # both ways, payload for one stream
    CLOSE = 5  # both ways, this stream is finished
    PING = 6  # relay -> phone, liveness and round-trip measurement
    PONG = 7  # phone -> relay


class RemoteLinkError(RuntimeError):
    """The tunnel could not be established or was violated."""


def encode_frame(
    frame_type: int,
    stream_id: int,
    port: int,
    payload: bytes,
    key: bytes,
) -> bytes:
    """Frame one message and authenticate the whole thing.

    The tag covers the header as well as the payload, so a stream id or port
    cannot be altered in flight to redirect traffic at another local service.
    """

    if len(payload) > MAX_PAYLOAD_BYTES:
        raise RemoteLinkError(f"payload of {len(payload)} bytes exceeds the limit")
    header = HEADER.pack(MAGIC, VERSION, frame_type, stream_id, port, len(payload))
    body = header + payload
    return body + hmac.new(key, body, hashlib.sha256).digest()


@dataclass(slots=True)
class Frame:
    type: int
    stream_id: int
    port: int
    payload: bytes


class FrameDecoder:
    """Accumulate bytes and yield whole authenticated frames."""

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            if len(self._buffer) < HEADER.size:
                return frames
            magic, version, frame_type, stream_id, port, length = HEADER.unpack(
                self._buffer[: HEADER.size]
            )
            if magic != MAGIC:
                raise RemoteLinkError("remote link frame had a bad magic")
            if version != VERSION:
                raise RemoteLinkError(f"unsupported remote link version {version}")
            if length > MAX_PAYLOAD_BYTES:
                raise RemoteLinkError("remote link frame exceeded the payload limit")
            total = HEADER.size + length + AUTH_TAG_BYTES
            if len(self._buffer) < total:
                return frames
            body = bytes(self._buffer[: HEADER.size + length])
            tag = bytes(self._buffer[HEADER.size + length : total])
            expected = hmac.new(self._key, body, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, tag):
                raise RemoteLinkError("remote link frame failed authentication")
            del self._buffer[:total]
            frames.append(
                Frame(
                    type=frame_type,
                    stream_id=stream_id,
                    port=port,
                    payload=body[HEADER.size :],
                )
            )


@dataclass(slots=True)
class RelayStats:
    phone_connected: bool = False
    connected_since: float = 0.0
    streams_open: int = 0
    streams_total: int = 0
    bytes_to_phone: int = 0
    bytes_from_phone: int = 0
    last_rtt_ms: float = 0.0
    reconnects: int = 0
    last_error: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "phone_connected": self.phone_connected,
            "connected_seconds": (
                round(time.monotonic() - self.connected_since, 1)
                if self.phone_connected
                else 0.0
            ),
            "streams_open": self.streams_open,
            "streams_total": self.streams_total,
            "bytes_to_phone": self.bytes_to_phone,
            "bytes_from_phone": self.bytes_from_phone,
            "last_rtt_ms": round(self.last_rtt_ms, 1),
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class _Stream:
    writer: asyncio.StreamWriter
    port: int


class RemoteLinkRelay:
    """Present a remote handset's gateway ports on this machine's loopback.

    One phone holds one tunnel. A second phone offering a tunnel while one is
    healthy is refused rather than silently taking over, because two handsets
    answering the same call would be worse than a rejected connection.
    """

    def __init__(
        self,
        key: bytes,
        *,
        listen_host: str = "0.0.0.0",
        listen_port: int = 8770,
        present_host: str = "127.0.0.1",
        ports: tuple[int, ...] = GATEWAY_PORTS,
        ping_interval: float = 5.0,
        phone_timeout: float = 20.0,
    ) -> None:
        if not key:
            raise RemoteLinkError("a remote link key is required")
        self._key = key
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._present_host = present_host
        self._ports = ports
        self._ping_interval = ping_interval
        self._phone_timeout = phone_timeout

        self.stats = RelayStats()
        self._phone_writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._streams: dict[int, _Stream] = {}
        self._next_stream = 1
        self._servers: list[asyncio.AbstractServer] = []
        self._tunnel_server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._last_pong = 0.0
        self._ping_sent_at: dict[int, float] = {}
        self._pending: deque[Frame] = deque()

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._tunnel_server = await asyncio.start_server(
            self._handle_phone, self._listen_host, self._listen_port
        )
        for port in self._ports:
            server = await asyncio.start_server(
                self._make_local_handler(port), self._present_host, port
            )
            self._servers.append(server)
        logger.info(
            "remote link relay listening on %s:%d, presenting %s on %s",
            self._listen_host,
            self._listen_port,
            ",".join(str(p) for p in self._ports),
            self._present_host,
        )

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for server in self._servers:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._servers.clear()
        if self._tunnel_server is not None:
            self._tunnel_server.close()
            with contextlib.suppress(Exception):
                await self._tunnel_server.wait_closed()
            self._tunnel_server = None
        await self._drop_phone("relay stopped")

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -------------------------------------------------------------- phone side

    async def _handle_phone(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        if self._phone_writer is not None and not self._phone_writer.is_closing():
            logger.warning("refusing a second phone tunnel from %s", peer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        decoder = FrameDecoder(self._key)
        self._pending.clear()
        try:
            # The first frame must be a valid HELLO. Anything else, including a
            # port scanner, is dropped before a tunnel is recorded.
            hello = await asyncio.wait_for(
                self._read_one(reader, decoder), timeout=10.0
            )
            if hello is None or hello.type != FrameType.HELLO:
                raise RemoteLinkError("first frame was not a HELLO")
        except (TimeoutError, RemoteLinkError, OSError) as exc:
            logger.warning("rejected tunnel from %s: %s", peer, exc)
            self.stats.last_error = str(exc)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        if self.stats.streams_total or self.stats.reconnects:
            self.stats.reconnects += 1
        self._phone_writer = writer
        self.stats.phone_connected = True
        self.stats.connected_since = time.monotonic()
        self.stats.last_error = ""
        self._last_pong = time.monotonic()
        logger.info("phone tunnel established from %s", peer)
        await self._send(FrameType.READY, 0, 0, b"")
        self._spawn(self._ping_loop(), "remote-link-ping")

        try:
            while True:
                frame = await self._read_one(reader, decoder)
                if frame is None:
                    break
                await self._on_phone_frame(frame)
        except (RemoteLinkError, OSError) as exc:
            logger.warning("phone tunnel failed: %s", exc)
            self.stats.last_error = str(exc)
        finally:
            await self._drop_phone("phone tunnel closed")

    async def _read_one(
        self, reader: asyncio.StreamReader, decoder: FrameDecoder
    ) -> Frame | None:
        """Return the next whole frame, buffering any that arrived with it.

        A single read routinely carries several frames. Dispatching the extras
        from inside this function would have run them before the HELLO was
        accepted, so they are queued and returned in order instead.
        """

        while True:
            if self._pending:
                return self._pending.popleft()
            chunk = await reader.read(65536)
            if not chunk:
                return None
            self.stats.bytes_from_phone += len(chunk)
            self._pending.extend(decoder.feed(chunk))

    async def _on_phone_frame(self, frame: Frame) -> None:
        if frame.type == FrameType.DATA:
            stream = self._streams.get(frame.stream_id)
            if stream is None:
                return
            try:
                stream.writer.write(frame.payload)
                await stream.writer.drain()
            except OSError:
                await self._close_stream(frame.stream_id, notify_phone=True)
        elif frame.type == FrameType.CLOSE:
            await self._close_stream(frame.stream_id, notify_phone=False)
        elif frame.type == FrameType.PONG:
            sent = self._ping_sent_at.pop(frame.stream_id, None)
            self._last_pong = time.monotonic()
            if sent is not None:
                self.stats.last_rtt_ms = (time.monotonic() - sent) * 1000

    async def _drop_phone(self, reason: str) -> None:
        writer = self._phone_writer
        self._phone_writer = None
        self.stats.phone_connected = False
        for stream_id in list(self._streams):
            await self._close_stream(stream_id, notify_phone=False)
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            logger.info("phone tunnel dropped: %s", reason)

    async def _send(
        self, frame_type: int, stream_id: int, port: int, payload: bytes
    ) -> bool:
        writer = self._phone_writer
        if writer is None or writer.is_closing():
            return False
        data = encode_frame(frame_type, stream_id, port, payload, self._key)
        async with self._write_lock:
            try:
                writer.write(data)
                await writer.drain()
            except OSError:
                return False
        self.stats.bytes_to_phone += len(data)
        return True

    async def _ping_loop(self) -> None:
        """Detect a phone that vanished without closing its socket."""

        while self._phone_writer is not None:
            await asyncio.sleep(self._ping_interval)
            if self._phone_writer is None:
                return
            if time.monotonic() - self._last_pong > self._phone_timeout:
                logger.warning("phone stopped answering pings; dropping the tunnel")
                await self._drop_phone("ping timeout")
                return
            token = secrets.randbelow(2**31) or 1
            self._ping_sent_at[token] = time.monotonic()
            if len(self._ping_sent_at) > 16:
                self._ping_sent_at.clear()
            await self._send(FrameType.PING, token, 0, b"")

    # -------------------------------------------------------------- local side

    def _make_local_handler(self, port: int) -> Any:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await self._handle_local(port, reader, writer)

        return handler

    async def _handle_local(
        self, port: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._phone_writer is None:
            # Refusing immediately is kinder than accepting and stalling: the
            # voice host reports "gateway unreachable" instead of hanging.
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        stream_id = self._next_stream
        self._next_stream = (self._next_stream + 1) % (2**31) or 1
        self._streams[stream_id] = _Stream(writer=writer, port=port)
        self.stats.streams_open = len(self._streams)
        self.stats.streams_total += 1

        if not await self._send(FrameType.OPEN, stream_id, port, b""):
            await self._close_stream(stream_id, notify_phone=False)
            return
        try:
            while True:
                chunk = await reader.read(MAX_PAYLOAD_BYTES)
                if not chunk:
                    break
                if not await self._send(FrameType.DATA, stream_id, port, chunk):
                    break
        except OSError:
            pass
        finally:
            await self._close_stream(stream_id, notify_phone=True)

    async def _close_stream(self, stream_id: int, *, notify_phone: bool) -> None:
        stream = self._streams.pop(stream_id, None)
        self.stats.streams_open = len(self._streams)
        if stream is None:
            return
        if notify_phone:
            await self._send(FrameType.CLOSE, stream_id, stream.port, b"")
        stream.writer.close()
        with contextlib.suppress(Exception):
            await stream.writer.wait_closed()


def load_remote_link_key() -> bytes:
    """The tunnel reuses the phone's existing shared secret.

    A second key would be a second thing to provision, rotate and get wrong, and
    the handset already holds this one.
    """

    configured = os.getenv("PHONE_AGENT_REMOTE_LINK_KEY_FILE", "").strip()
    candidates = [configured] if configured else []
    candidates.append(os.getenv("PHONE_AGENT_LINK_KEY_FILE", "").strip())
    candidates.append(str(Path.home() / ".config" / "phone-agent" / "link.key"))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = open(candidate, "rb").read().strip()
        except OSError:
            continue
        if len(data) >= 16:
            return data
    raise RemoteLinkError(
        "no remote link key found; provision one with "
        "./android_service_apk/provision_link_key.sh"
    )


@dataclass(slots=True)
class RemoteLinkSettings:
    """How this machine offers the tunnel to a handset."""

    enabled: bool = False
    listen_host: str = "0.0.0.0"
    listen_port: int = 8770
    ports: tuple[int, ...] = field(default=GATEWAY_PORTS)

    @classmethod
    def from_env(cls) -> RemoteLinkSettings:
        enabled = os.getenv("PHONE_AGENT_REMOTE_LINK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            enabled=enabled,
            listen_host=os.getenv("PHONE_AGENT_REMOTE_LINK_HOST", "0.0.0.0").strip(),
            listen_port=int(os.getenv("PHONE_AGENT_REMOTE_LINK_PORT", "8770")),
        )
