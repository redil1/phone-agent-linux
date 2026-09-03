"""Carry the four phone gateway ports over one outbound connection.

The Mac reaches the handset today through ``adb forward``, which needs a USB
cable and a machine standing next to the phone. Every call operation is already
plain TCP -- dial, hangup and status are HTTP on 8765, and the media path is
three sockets on 8766-8768 -- so the cable is only a transport, not a
capability. Replacing it lets the runtime live anywhere.

A handset on mobile data usually sits behind carrier NAT, so a server cannot
open a connection to it. The phone therefore dials out. Protocol v2 keeps one
coordinator and gives every accepted gateway connection its own outbound TCP
tunnel, preventing capture backpressure from starving control or playout ACKs.
The relay then re-presents the ports on its own loopback, which is exactly the
shape ``adb forward`` produced, so the voice host needs no change at all: it
still talks to 127.0.0.1:8765-8768 and cannot tell the difference. Installed v1
phones retain their original multiplexed single-socket transport.

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
import pathlib
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("PhoneAgentRemoteLink")

MAGIC = b"PHRL"
# ``VERSION`` remains the v1 default for callers which use the original
# five-argument ``encode_frame`` API.  Protocol v2 is selected explicitly by
# the handset during its HELLO, which lets an installed v1 APK keep working
# while newer handsets move every gateway stream onto its own carrier flow.
VERSION = 1
VERSION_V2 = 2
SUPPORTED_VERSIONS = frozenset((VERSION, VERSION_V2))
AUTH_TAG_BYTES = hashlib.sha256().digest_size
# One media frame is 640 bytes; this bounds a hostile or broken peer without
# ever truncating legitimate traffic.
MAX_PAYLOAD_BYTES = 64 * 1024
HEADER = struct.Struct("!4sBBIHI")  # magic, version, type, stream, port, length

# The gateway ports, in the order the relay presents them.
GATEWAY_PORTS: tuple[int, ...] = (8765, 8766, 8767, 8768)

# Cross-platform timeout contract. Android allows an outbound data carrier up
# to 15 seconds to connect. The relay must retain the authenticated OPEN longer
# than that, and the local runtime must in turn wait longer than the relay.
PHONE_STREAM_CONNECT_TIMEOUT_SECONDS = 15.0
V2_STREAM_ATTACH_TIMEOUT_SECONDS = 20.0


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
    *,
    version: int = VERSION,
) -> bytes:
    """Frame one message and authenticate the whole thing.

    The tag covers the header as well as the payload, so a stream id or port
    cannot be altered in flight to redirect traffic at another local service.
    """

    if len(payload) > MAX_PAYLOAD_BYTES:
        raise RemoteLinkError(f"payload of {len(payload)} bytes exceeds the limit")
    header = HEADER.pack(MAGIC, version, frame_type, stream_id, port, len(payload))
    body = header + payload
    return body + hmac.new(key, body, hashlib.sha256).digest()


@dataclass(slots=True)
class Frame:
    type: int
    stream_id: int
    port: int
    payload: bytes
    version: int = VERSION


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
            if version not in SUPPORTED_VERSIONS:
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
                    version=version,
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
    protocol_version: int = 0
    stream_attach_timeout_seconds: float = V2_STREAM_ATTACH_TIMEOUT_SECONDS
    last_stream_attach_ms: float = 0.0
    stream_attach_timeouts: int = 0

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
            "protocol_version": self.protocol_version,
            "stream_attach_timeout_seconds": self.stream_attach_timeout_seconds,
            "last_stream_attach_ms": round(self.last_stream_attach_ms, 1),
            "stream_attach_timeouts": self.stream_attach_timeouts,
        }


@dataclass(slots=True)
class _Stream:
    stream_id: int
    writer: asyncio.StreamWriter
    port: int
    protocol_version: int
    coordinator_writer: asyncio.StreamWriter
    challenge: bytes = b""
    tunnel_writer: asyncio.StreamWriter | None = None
    tunnel_ready: asyncio.Event = field(default_factory=asyncio.Event)
    tunnel_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    opened_at: float = field(default_factory=time.monotonic)


class RemoteLinkRelay:
    """Present a remote handset's gateway ports on this machine's loopback.

    One phone holds one coordinator plus its authenticated v2 stream tunnels.
    A second coordinator is refused rather than silently taking over, because
    two handsets answering the same call would be worse than a rejection.
    """

    def __init__(
        self,
        key: bytes,
        *,
        listen_host: str = "0.0.0.0",
        listen_port: int = 8770,
        present_host: str = "127.0.0.1",
        ports: tuple[int, ...] | None = None,
        ping_interval: float = 5.0,
        phone_timeout: float = 20.0,
        stream_attach_timeout: float = V2_STREAM_ATTACH_TIMEOUT_SECONDS,
    ) -> None:
        if not key:
            raise RemoteLinkError("a remote link key is required")
        if stream_attach_timeout <= 0:
            raise RemoteLinkError("v2 stream attach timeout must be positive")
        self._key = key
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._present_host = present_host
        # Resolved here rather than as a default argument so the module
        # constant stays overridable at runtime.
        self._ports = ports if ports is not None else GATEWAY_PORTS
        self._ping_interval = ping_interval
        self._phone_timeout = phone_timeout
        self._stream_attach_timeout = stream_attach_timeout

        self.stats = RelayStats()
        self.stats.stream_attach_timeout_seconds = stream_attach_timeout
        self._phone_writer: asyncio.StreamWriter | None = None
        self._phone_version = VERSION
        self._write_lock = asyncio.Lock()
        self._phone_admission_lock = asyncio.Lock()
        self._streams: dict[int, _Stream] = {}
        self._next_stream = 1
        self._servers: list[asyncio.AbstractServer] = []
        self._tunnel_server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._last_pong = 0.0
        self._ping_sent_at: dict[int, float] = {}

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._tunnel_server = await asyncio.start_server(
            self._handle_phone, self._listen_host, self._listen_port
        )
        for port in self._ports:
            try:
                server = await asyncio.start_server(
                    self._make_local_handler(port), self._present_host, port
                )
            except OSError as exc:
                await self.close()
                # An adb forward holds exactly these ports, so this is the most
                # likely failure by far and the message has to say so.
                raise RemoteLinkError(
                    f"gateway port {port} is already in use ({exc.strerror}). "
                    "A USB forward is probably still active; run "
                    "'adb forward --remove-all' or unplug the phone first."
                ) from exc
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
        decoder = FrameDecoder(self._key)
        pending: deque[Frame] = deque()
        try:
            # The first frame must be a valid HELLO. Anything else, including a
            # port scanner, is dropped before a tunnel is recorded.
            hello = await asyncio.wait_for(
                self._read_one(reader, decoder, pending), timeout=10.0
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

        # A v2 data tunnel authenticates exactly like the coordinator, but its
        # HELLO names the OPEN it belongs to.  It must be classified only after
        # authentication; otherwise an unauthenticated connection could occupy
        # or interfere with a live stream.
        if hello.version == VERSION_V2 and (hello.stream_id or hello.port):
            await self._handle_v2_data_tunnel(reader, writer, decoder, pending, hello)
            return

        if hello.stream_id != 0 or hello.port != 0:
            await self._reject_writer(
                writer,
                f"invalid coordinator HELLO from {peer}: "
                f"stream={hello.stream_id} port={hello.port}",
            )
            return

        async with self._phone_admission_lock:
            if self._phone_writer is not None and not self._phone_writer.is_closing():
                await self._reject_writer(writer, f"refusing a second phone tunnel from {peer}")
                return
            if self.stats.streams_total or self.stats.reconnects:
                self.stats.reconnects += 1
            self._phone_writer = writer
            self._phone_version = hello.version
            self.stats.phone_connected = True
            self.stats.connected_since = time.monotonic()
            self.stats.last_error = ""
            self.stats.protocol_version = hello.version
            self._last_pong = time.monotonic()

        logger.info("phone v%d coordinator established from %s", hello.version, peer)
        if not await self._send_coordinator(
            FrameType.READY,
            0,
            0,
            b"",
            expected_writer=writer,
            version=hello.version,
        ):
            await self._drop_phone("READY failed", expected_writer=writer)
            return
        self._spawn(self._ping_loop(writer), "remote-link-ping")

        try:
            while True:
                frame = await self._read_one(reader, decoder, pending)
                if frame is None:
                    break
                if frame.version != hello.version:
                    raise RemoteLinkError("remote link changed protocol version mid-connection")
                await self._on_phone_frame(frame, hello.version)
        except (RemoteLinkError, OSError) as exc:
            logger.warning("phone tunnel failed: %s", exc)
            self.stats.last_error = str(exc)
        finally:
            await self._drop_phone("phone tunnel closed", expected_writer=writer)

    async def _read_one(
        self,
        reader: asyncio.StreamReader,
        decoder: FrameDecoder,
        pending: deque[Frame],
    ) -> Frame | None:
        """Return the next whole frame, buffering any that arrived with it.

        A single read routinely carries several frames. Dispatching the extras
        from inside this function would have run them before the HELLO was
        accepted, so they are queued and returned in order instead.
        """

        while True:
            if pending:
                return pending.popleft()
            chunk = await reader.read(65536)
            if not chunk:
                return None
            self.stats.bytes_from_phone += len(chunk)
            pending.extend(decoder.feed(chunk))

    async def _on_phone_frame(self, frame: Frame, version: int) -> None:
        if frame.type == FrameType.DATA:
            if version == VERSION_V2:
                raise RemoteLinkError("v2 DATA is forbidden on the coordinator")
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

    async def _handle_v2_data_tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        decoder: FrameDecoder,
        pending: deque[Frame],
        hello: Frame,
    ) -> None:
        """Bind one authenticated WAN connection to one pending v2 OPEN.

        The coordinator never carries DATA in v2.  Consequently backpressure
        on capture, playout, control, or acknowledgement traffic is contained
        to that gateway connection's TCP flow instead of stalling every port.
        """

        stream = self._streams.get(hello.stream_id)
        coordinator = self._phone_writer
        if (
            coordinator is None
            or self._phone_version != VERSION_V2
            or stream is None
            or stream.protocol_version != VERSION_V2
            or stream.coordinator_writer is not coordinator
            or stream.port != hello.port
            or stream.tunnel_writer is not None
            or not hmac.compare_digest(stream.challenge, hello.payload)
        ):
            await self._reject_writer(
                writer,
                "refusing an unrequested or mismatched v2 data tunnel "
                f"stream={hello.stream_id} port={hello.port}",
            )
            return

        stream.tunnel_writer = writer
        if not await self._send_stream(stream, FrameType.READY, b""):
            await self._close_stream(hello.stream_id, notify_phone=False)
            return
        stream.tunnel_ready.set()
        self.stats.last_stream_attach_ms = (time.monotonic() - stream.opened_at) * 1000
        self.stats.last_error = ""
        attach_log = (
            logger.warning if self.stats.last_stream_attach_ms >= 2_000 else logger.debug
        )
        attach_log(
            "v2 data tunnel attached stream=%d port=%d elapsed_ms=%.1f",
            hello.stream_id,
            hello.port,
            self.stats.last_stream_attach_ms,
        )

        try:
            while self._streams.get(hello.stream_id) is stream:
                frame = await self._read_one(reader, decoder, pending)
                if frame is None:
                    break
                if frame.version != VERSION_V2:
                    raise RemoteLinkError("v2 data tunnel changed protocol version")
                if frame.stream_id != hello.stream_id or frame.port != hello.port:
                    raise RemoteLinkError("v2 data tunnel frame identity did not match HELLO")
                if frame.type == FrameType.DATA:
                    try:
                        stream.writer.write(frame.payload)
                        await stream.writer.drain()
                    except OSError:
                        break
                elif frame.type == FrameType.CLOSE:
                    await self._close_stream(hello.stream_id, notify_phone=False)
                    return
                else:
                    raise RemoteLinkError(
                        f"frame type {frame.type} is forbidden on a v2 data tunnel"
                    )
        except (RemoteLinkError, OSError) as exc:
            logger.warning(
                "v2 data tunnel failed stream=%d port=%d: %s",
                hello.stream_id,
                hello.port,
                exc,
            )
            self.stats.last_error = str(exc)
        finally:
            if self._streams.get(hello.stream_id) is stream:
                await self._close_stream(hello.stream_id, notify_phone=False)

    async def _drop_phone(
        self,
        reason: str,
        *,
        expected_writer: asyncio.StreamWriter | None = None,
    ) -> None:
        writer = self._phone_writer
        if expected_writer is not None and writer is not expected_writer:
            return
        self._phone_writer = None
        self.stats.phone_connected = False
        self.stats.protocol_version = 0
        for stream_id in list(self._streams):
            await self._close_stream(stream_id, notify_phone=False)
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            logger.info("phone tunnel dropped: %s", reason)

    async def _reject_writer(self, writer: asyncio.StreamWriter, reason: str) -> None:
        logger.warning("%s", reason)
        self.stats.last_error = reason
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _write_frame(
        self,
        writer: asyncio.StreamWriter,
        lock: asyncio.Lock,
        frame_type: int,
        stream_id: int,
        port: int,
        payload: bytes,
        *,
        version: int,
    ) -> bool:
        if writer.is_closing():
            return False
        data = encode_frame(
            frame_type,
            stream_id,
            port,
            payload,
            self._key,
            version=version,
        )
        async with lock:
            try:
                writer.write(data)
                await writer.drain()
            except OSError:
                return False
        self.stats.bytes_to_phone += len(data)
        return True

    async def _send_coordinator(
        self,
        frame_type: int,
        stream_id: int,
        port: int,
        payload: bytes,
        *,
        expected_writer: asyncio.StreamWriter | None = None,
        version: int | None = None,
    ) -> bool:
        writer = self._phone_writer
        if writer is None or (expected_writer is not None and writer is not expected_writer):
            return False
        return await self._write_frame(
            writer,
            self._write_lock,
            frame_type,
            stream_id,
            port,
            payload,
            version=self._phone_version if version is None else version,
        )

    async def _send_stream(
        self, stream: _Stream, frame_type: int, payload: bytes
    ) -> bool:
        writer = stream.tunnel_writer
        if writer is None:
            return False
        return await self._write_frame(
            writer,
            stream.tunnel_write_lock,
            frame_type,
            stream_id=stream.stream_id,
            port=stream.port,
            payload=payload,
            version=VERSION_V2,
        )

    async def _ping_loop(self, coordinator: asyncio.StreamWriter) -> None:
        """Detect a phone that vanished without closing its socket."""

        while self._phone_writer is coordinator:
            await asyncio.sleep(self._ping_interval)
            if self._phone_writer is not coordinator:
                return
            if time.monotonic() - self._last_pong > self._phone_timeout:
                logger.warning("phone stopped answering pings; dropping the tunnel")
                await self._drop_phone("ping timeout", expected_writer=coordinator)
                return
            token = secrets.randbelow(2**31) or 1
            self._ping_sent_at[token] = time.monotonic()
            if len(self._ping_sent_at) > 16:
                self._ping_sent_at.clear()
            await self._send_coordinator(
                FrameType.PING, token, 0, b"", expected_writer=coordinator
            )

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
        coordinator = self._phone_writer
        if coordinator is None:
            # Refusing immediately is kinder than accepting and stalling: the
            # voice host reports "gateway unreachable" instead of hanging.
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        stream_id = self._next_stream
        self._next_stream = (self._next_stream + 1) % (2**31) or 1
        protocol_version = self._phone_version
        stream = _Stream(
            stream_id=stream_id,
            writer=writer,
            port=port,
            protocol_version=protocol_version,
            coordinator_writer=coordinator,
            challenge=secrets.token_bytes(32) if protocol_version == VERSION_V2 else b"",
        )
        self._streams[stream_id] = stream
        self.stats.streams_open = len(self._streams)
        self.stats.streams_total += 1

        if not await self._send_coordinator(
            FrameType.OPEN,
            stream_id,
            port,
            stream.challenge,
            expected_writer=coordinator,
            version=protocol_version,
        ):
            await self._close_stream(stream_id, notify_phone=False)
            return
        if protocol_version == VERSION_V2:
            try:
                await asyncio.wait_for(
                    stream.tunnel_ready.wait(), timeout=self._stream_attach_timeout
                )
            except TimeoutError:
                self.stats.stream_attach_timeouts += 1
                self.stats.last_error = (
                    f"v2 data tunnel timed out after {self._stream_attach_timeout:.1f}s "
                    f"stream={stream_id} port={port}"
                )
                await self._close_stream(stream_id, notify_phone=False)
                return
            if self._streams.get(stream_id) is not stream or stream.tunnel_writer is None:
                return
        try:
            while True:
                chunk = await reader.read(MAX_PAYLOAD_BYTES)
                if not chunk:
                    break
                sent = (
                    await self._send_stream(stream, FrameType.DATA, chunk)
                    if protocol_version == VERSION_V2
                    else await self._send_coordinator(
                        FrameType.DATA,
                        stream_id,
                        port,
                        chunk,
                        expected_writer=coordinator,
                        version=protocol_version,
                    )
                )
                if not sent:
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
        # Wake a local handler which is waiting for its v2 data connection so
        # coordinator loss cannot strand it until the full attach timeout.
        stream.tunnel_ready.set()
        if notify_phone:
            if stream.protocol_version == VERSION_V2 and stream.tunnel_writer is not None:
                await self._write_frame(
                    stream.tunnel_writer,
                    stream.tunnel_write_lock,
                    FrameType.CLOSE,
                    stream_id,
                    stream.port,
                    b"",
                    version=VERSION_V2,
                )
            else:
                # A v2 handset can reject an OPEN on the coordinator, and the
                # relay mirrors that path when the local peer closes before the
                # dedicated data tunnel finishes attaching.
                await self._send_coordinator(
                    FrameType.CLOSE,
                    stream_id,
                    stream.port,
                    b"",
                    expected_writer=stream.coordinator_writer,
                    version=stream.protocol_version,
                )
        if stream.tunnel_writer is not None:
            stream.tunnel_writer.close()
            with contextlib.suppress(Exception):
                await stream.tunnel_writer.wait_closed()
        stream.writer.close()
        with contextlib.suppress(Exception):
            await stream.writer.wait_closed()


def local_addresses() -> list[str]:
    """Addresses on this machine a handset could reach, best guess first.

    Studio shows these so an operator can read one off the screen instead of
    hunting for an IP on the command line.
    """

    import socket

    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with contextlib.closing(probe):
            # No packet is sent; this just asks the routing table which local
            # address would be used to reach the internet.
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found and not address.startswith("127."):
                found.append(address)
    except OSError:
        pass
    return found


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
            # Read the key exactly as written. .strip() here treated 0x20, 0x0a
            # and the other whitespace bytes as padding, but they are ordinary
            # key material: every writer emits raw bytes with no trailing
            # newline, so a key that happened to begin or end with one of them
            # -- about one in twenty -- loaded shorter and different than the
            # one the handset scanned, and the tunnel then failed to
            # authenticate with no indication why.
            data = pathlib.Path(candidate).read_bytes()
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
    ports: tuple[int, ...] = field(default_factory=lambda: GATEWAY_PORTS)

    @staticmethod
    def store_path() -> Path:
        return Path.home() / ".config" / "phone-agent" / "remote-link.json"

    @classmethod
    def load(cls) -> RemoteLinkSettings:
        """Operator choice first, environment only as the initial default.

        Turning the tunnel on used to need an environment variable, which meant
        restarting the service from a terminal. Persisting the choice is what
        lets Studio own it.
        """

        import json

        settings = cls.from_env()
        try:
            stored = json.loads(cls.store_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return settings
        if isinstance(stored, dict):
            settings.enabled = bool(stored.get("enabled", settings.enabled))
            settings.listen_port = int(stored.get("listen_port", settings.listen_port))
            settings.listen_host = str(stored.get("listen_host", settings.listen_host))
        return settings

    def save(self) -> None:
        import json

        path = self.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "enabled": self.enabled,
                    "listen_host": self.listen_host,
                    "listen_port": self.listen_port,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

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
