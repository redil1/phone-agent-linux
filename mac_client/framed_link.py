"""Authenticated production link between the Mac runtime and Android gateway."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from phone_agent_gateway.ai_bridge.media_protocol import (
    FrameDirection,
    FrameFlags,
    FrameKind,
    FrameStreamDecoder,
    MediaFrame,
    MediaProtocolError,
    encode_frame,
)
from phone_agent_gateway.ai_bridge.session import CallSessionState, GenerationAdvance

logger = logging.getLogger("PhoneAgentFramedLink")

# Must exceed the relay's 20-second v2 attach budget. A local runtime socket
# must not abandon an OPEN while Android can still complete its authenticated
# WAN carrier, otherwise the eventual data HELLO becomes an orphan.
REMOTE_STREAM_HANDSHAKE_TIMEOUT_SECONDS = 25.0


class LinkError(RuntimeError):
    """Base authenticated-link error."""


class LinkDisconnected(LinkError):
    """The Android peer is not connected or stopped responding."""


class LinkRejected(LinkError):
    """Android rejected a handshake or command."""


@dataclass(frozen=True, slots=True)
class LinkPorts:
    legacy_http: int = 8765
    downlink: int = 8766
    uplink: int = 8767
    control: int = 8768


@dataclass(slots=True)
class _Channel:
    name: str
    sock: socket.socket
    decoder: FrameStreamDecoder
    pending: deque[MediaFrame]


AudioFrameCallback = Callable[[MediaFrame], None]


def _port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Whether something is already accepting connections on this port."""

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class FramedGatewayLink:
    """Own three independently backpressured authenticated TCP channels.

    The control channel remains independent from both media directions so an
    urgent flush or hangup cannot wait behind TTS bytes. All three sockets are
    bound to the same call ID and link epoch by the first authenticated frame.
    """

    UPLINK_WINDOW_FRAMES = 60
    UPLINK_ACK_TIMEOUT_SECS = 15.0

    def __init__(
        self,
        session: CallSessionState,
        authentication_key: bytes,
        *,
        host: str = "127.0.0.1",
        ports: LinkPorts | None = None,
        remote_ports: LinkPorts | None = None,
        device_id: str | None = None,
        auto_forward_adb: bool = True,
    ) -> None:
        if len(authentication_key) < 32:
            raise ValueError("authentication_key must contain at least 32 bytes")
        self.session = session
        self.authentication_key = authentication_key
        self.host = host
        self.ports = ports or LinkPorts()
        self.remote_ports = remote_ports or LinkPorts()
        self.device_id = device_id
        self.auto_forward_adb = auto_forward_adb

        self._state_lock = threading.RLock()
        self._tx_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._channels: dict[str, _Channel] = {}
        self._audio_callbacks: list[AudioFrameCallback] = []
        self._rx_thread: threading.Thread | None = None
        self._uplink_ack_thread: threading.Thread | None = None
        self._supervisor_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._disconnected = threading.Event()
        self._control_connected = threading.Event()
        self._media_connected = threading.Event()
        self._want_media = False
        self._control_sequence = 0
        self._uplink_credit = threading.Condition()
        self._uplink_credit_generation = session.generation_id
        self._uplink_pending: deque[tuple[int, int]] = deque()
        self._uplink_timestamps: dict[tuple[int, int], float] = {}
        self._rtt_ema: float = 0.150

    @property
    def connected(self) -> bool:
        return self._control_connected.is_set()

    @property
    def media_connected(self) -> bool:
        return self._media_connected.is_set()

    def on_audio_received(self, callback: AudioFrameCallback) -> None:
        self._audio_callbacks.append(callback)

    def ensure_adb_forward(self) -> None:
        # The remote relay publishes all four gateway ports as one bundle.  A
        # plain TCP "is it open?" probe is safe only for the legacy HTTP port:
        # the framed media ports interpret every accepted socket as a protocol
        # session, and the Android uplink creates a physical Telephony-TX track
        # for it.  Probing 8767 while a cellular call is active can therefore
        # consume/poison a scarce vendor mixer slot before authentication.
        relay_available = False
        port_pairs = zip(
            (
                self.ports.legacy_http,
                self.ports.downlink,
                self.ports.uplink,
                self.ports.control,
            ),
            (
                self.remote_ports.legacy_http,
                self.remote_ports.downlink,
                self.remote_ports.uplink,
                self.remote_ports.control,
            ),
            strict=True,
        )
        for local_port, remote_port in port_pairs:
            if relay_available:
                logger.info(
                    "gateway port %d is presented by the remote relay bundle",
                    local_port,
                )
                continue
            command = ["adb"]
            if self.device_id:
                command.extend(["-s", self.device_id])
            command.extend(
                ["forward", f"tcp:{local_port}", f"tcp:{remote_port}"]
            )
            try:
                subprocess.run(command, check=True, capture_output=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                # Probe only HTTP, then trust the relay's all-or-nothing port
                # bundle. Never make an unauthenticated connection to a framed
                # control or media listener merely to test reachability.
                if local_port == self.ports.legacy_http and _port_is_open(
                    self.host, local_port
                ):
                    relay_available = True
                    logger.info(
                        "gateway ports are already served by the remote relay "
                        "(HTTP port %d verified)",
                        local_port,
                    )
                    continue
                raise LinkDisconnected(
                    f"could not forward local {local_port} to Android {remote_port}: {exc}"
                ) from exc

    def connect(self, timeout: float = 3.0) -> None:
        """Connect and authenticate all channels for the current link epoch."""

        self.connect_control(timeout=timeout)
        self.connect_media(timeout=timeout)

    def connect_control(
        self, timeout: float = REMOTE_STREAM_HANDSHAKE_TIMEOUT_SECONDS
    ) -> None:
        """Connect only urgent control; safe before a cellular call is active."""

        if self.auto_forward_adb:
            self.ensure_adb_forward()
        self._close_channels()
        self._disconnected.clear()
        try:
            control = self._open_channel("control", self.ports.control, timeout)
        except Exception:
            raise
        with self._state_lock:
            self._channels = {"control": control}
            self._control_connected.set()
        logger.info(
            "authenticated phone control connected call_id=%s epoch=%s generation=%d",
            self.session.call_id,
            self.session.link_epoch,
            self.session.generation_id,
        )

    def connect_media(
        self, timeout: float = REMOTE_STREAM_HANDSHAKE_TIMEOUT_SECONDS
    ) -> None:
        """Attach authenticated media only after Telecom reports ACTIVE."""

        if not self.connected:
            raise LinkDisconnected("control channel must connect before media")
        self._want_media = True
        opened: dict[str, _Channel] = {}
        try:
            opened["uplink"] = self._open_channel("uplink", self.ports.uplink, timeout)
            opened["downlink"] = self._open_channel("downlink", self.ports.downlink, timeout)
        except Exception:
            for channel in opened.values():
                self._close_socket(channel.sock)
            raise
        with self._state_lock:
            self._channels.update(opened)
            self._media_connected.set()
        self._reset_uplink_credit(self.session.generation_id)
        self._rx_thread = threading.Thread(
            target=self._receive_downlink,
            name="phoneagent-framed-downlink",
            daemon=True,
        )
        self._uplink_ack_thread = threading.Thread(
            target=self._receive_uplink_acks,
            name="phoneagent-framed-uplink-acks",
            daemon=True,
        )
        self._rx_thread.start()
        self._uplink_ack_thread.start()
        logger.info(
            "authenticated phone media connected call_id=%s epoch=%s generation=%d",
            self.session.call_id,
            self.session.link_epoch,
            self.session.generation_id,
        )

    def reconnect(self, timeout: float = 10.0) -> None:
        self._close_channels()
        self.session.reconnect()
        self.connect_control(timeout=timeout)
        if self._want_media:
            self.connect_media(timeout=timeout)

    def start_supervisor(self) -> None:
        """Maintain the authenticated link across USB/ADB disconnects."""

        with self._state_lock:
            if self._supervisor_thread and self._supervisor_thread.is_alive():
                return
            self._stop.clear()
            self._supervisor_thread = threading.Thread(
                target=self._supervise,
                name="phoneagent-link-supervisor",
                daemon=True,
            )
            self._supervisor_thread.start()

    def _supervise(self) -> None:
        delay = 0.25
        first_attempt = True
        while not self._stop.is_set():
            if not first_attempt:
                try:
                    self.session.reconnect()
                except Exception:
                    logger.exception("cannot create a new link epoch")
                    return
            first_attempt = False
            try:
                self.connect_control()
                if self._want_media:
                    self.connect_media()
                delay = 0.25
                while not self._stop.is_set() and not self._disconnected.wait(0.25):
                    pass
            except Exception as exc:
                if not self._stop.is_set():
                    logger.warning("phone link unavailable; retrying in %.2fs: %s", delay, exc)
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, 5.0)

    def send_audio_chunk(self, pcm_bytes: bytes, generation_id: int, sequence: int) -> None:
        if not pcm_bytes:
            return
        frame = MediaFrame(
            kind=FrameKind.AUDIO,
            direction=FrameDirection.MAC_TO_PHONE,
            call_id=self.session.call_id,
            generation_id=generation_id,
            sequence=sequence,
            monotonic_ns=time.monotonic_ns(),
            payload=pcm_bytes,
            sample_rate=16_000,
            channels=1,
            sample_width=2,
        )
        self._send_uplink_frame(frame, "phone uplink write failed")

    def send_audio_end_marker(self, generation_id: int, sequence: int) -> None:
        """End one speech segment in wire order without relying on queue starvation."""

        frame = MediaFrame(
            kind=FrameKind.CONTROL,
            direction=FrameDirection.MAC_TO_PHONE,
            call_id=self.session.call_id,
            generation_id=generation_id,
            sequence=sequence,
            monotonic_ns=time.monotonic_ns(),
            payload=b"",
            flags=FrameFlags.END_OF_STREAM,
        )
        self._send_uplink_frame(frame, "phone audio-end marker failed")

    def request(
        self,
        command_type: str,
        payload: dict[str, Any] | None = None,
        *,
        command_id: UUID | None = None,
        urgent: bool = False,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Send an idempotent command and wait for its matching acknowledgement."""

        identifier = command_id or uuid4()
        message = {
            "type": command_type,
            "command_id": str(identifier),
            "link_epoch": str(self.session.link_epoch),
            "payload": payload or {},
        }
        with self._control_lock:
            channel = self._require_channel("control")
            sequence = self._control_sequence
            self._control_sequence += 1
            frame = MediaFrame.control(
                direction=FrameDirection.MAC_TO_PHONE,
                call_id=self.session.call_id,
                generation_id=self.session.generation_id,
                sequence=sequence,
                monotonic_ns=time.monotonic_ns(),
                message=message,
                urgent=urgent,
            )
            original_timeout = channel.sock.gettimeout()
            channel.sock.settimeout(timeout)
            try:
                channel.sock.sendall(
                    encode_frame(frame, authentication_key=self.authentication_key)
                )
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"control command {command_type} acknowledgement timed out"
                        )
                    channel.sock.settimeout(remaining)
                    candidate = self._receive_one(channel)
                    candidate_body = candidate.json_payload()
                    if candidate_body.get("command_id") == str(identifier):
                        response = candidate
                        body = candidate_body
                        break
                    # A previous request can time out after Android executed it,
                    # leaving its late acknowledgement in this ordered socket.
                    # It is stale, not a reply to the current command. Continue
                    # until the matching command id arrives instead of poisoning
                    # every subsequent status poll with mismatch errors.
                    logger.warning(
                        "discarding stale control acknowledgement command_id=%s "
                        "while waiting for %s",
                        candidate_body.get("command_id", "<missing>"),
                        identifier,
                    )
            except (OSError, TimeoutError, MediaProtocolError) as exc:
                if not urgent and command_type not in {"call.status", "gateway.health", "audio.flush", "audio.status", "audio.reset"}:
                    self._mark_disconnected(exc)
                raise LinkDisconnected(f"control command {command_type} failed") from exc
            finally:
                try:
                    channel.sock.settimeout(original_timeout)
                except OSError:
                    pass
        if response.kind is FrameKind.ERROR or body.get("status") != "ok":
            raise LinkRejected(str(body.get("message") or body))
        return body

    def flush_audio(self, advance: GenerationAdvance) -> dict[str, Any]:
        try:
            result = self.request(
                "audio.flush",
                {
                    "cancelled_generation": advance.cancelled_generation,
                    "next_generation": advance.next_generation,
                    "reason": advance.reason,
                },
                urgent=True,
                timeout=5.0,
            )
            acknowledged = int(result.get("generation", 0))
            if acknowledged < advance.next_generation:
                logger.warning(
                    "phone acknowledged flush for generation %d instead of %d",
                    acknowledged,
                    advance.next_generation,
                )
            else:
                self.session.resynchronize_generation(acknowledged)
                self._reset_uplink_credit(acknowledged)
            return result
        except Exception as exc:
            logger.warning("phone audio flush non-fatal error: %s", exc)
            self.session.resynchronize_generation(advance.next_generation)
            self._reset_uplink_credit(advance.next_generation)
            return {"status": "ok", "generation": advance.next_generation}

    def close(self) -> None:
        self._stop.set()
        self._close_channels()
        supervisor = self._supervisor_thread
        if supervisor and supervisor.is_alive() and supervisor is not threading.current_thread():
            supervisor.join(timeout=2.0)
        self._supervisor_thread = None

    def _open_channel(self, name: str, port: int, timeout: float) -> _Channel:
        sock = socket.create_connection((self.host, port), timeout=timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if name == "uplink":
                # Bound cancelled speech buffered below the application queue.
                # The control channel remains independent for immediate flush.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024)
            channel = _Channel(
                name=name,
                sock=sock,
                decoder=FrameStreamDecoder(authentication_key=self.authentication_key),
                pending=deque(),
            )
            hello = MediaFrame.control(
                direction=FrameDirection.BIDIRECTIONAL,
                call_id=self.session.call_id,
                generation_id=self.session.generation_id,
                sequence=0,
                monotonic_ns=time.monotonic_ns(),
                message={
                    "type": "gateway.hello",
                    "link_epoch": str(self.session.link_epoch),
                    "channel": name,
                },
                urgent=True,
            )
            sock.sendall(encode_frame(hello, authentication_key=self.authentication_key))
            response = self._receive_one(channel)
            body = response.json_payload()
            if response.kind is not FrameKind.ACK or body.get("type") != "gateway.ready":
                raise LinkRejected(f"Android rejected {name} handshake: {body}")
            if response.call_id != self.session.call_id:
                raise LinkRejected(f"Android returned the wrong call ID for {name}")
            if body.get("link_epoch") != str(self.session.link_epoch):
                raise LinkRejected(f"Android returned the wrong link epoch for {name}")
            self.session.resynchronize_generation(int(body.get("generation", 1)))
            sock.settimeout(2.0)
            return channel
        except Exception:
            self._close_socket(sock)
            raise

    def _receive_downlink(self) -> None:
        try:
            channel = self._require_channel("downlink")
            while not self._stop.is_set() and self.media_connected:
                try:
                    frame = self._receive_one(channel)
                except TimeoutError:
                    continue
                if frame.kind is not FrameKind.AUDIO:
                    logger.warning("ignoring non-audio frame on downlink: %s", frame.kind.name)
                    continue
                if frame.direction is not FrameDirection.PHONE_TO_MAC:
                    raise MediaProtocolError("downlink frame has the wrong direction")
                if frame.call_id != self.session.call_id:
                    raise MediaProtocolError("downlink frame belongs to a different call")
                for callback in tuple(self._audio_callbacks):
                    try:
                        callback(frame)
                    except Exception:
                        logger.exception("phone downlink callback failed")
        except (OSError, MediaProtocolError, LinkError) as exc:
            if not self._stop.is_set():
                self._mark_disconnected(exc)

    def _receive_uplink_acks(self) -> None:
        """Release one send credit only after Android rendered that wire frame."""

        try:
            channel = self._require_channel("uplink")
            while not self._stop.is_set() and self.media_connected:
                try:
                    frame = self._receive_one(channel)
                except TimeoutError:
                    continue
                if frame.kind is not FrameKind.ACK:
                    raise MediaProtocolError("uplink returned a non-acknowledgement frame")
                if frame.direction is not FrameDirection.PHONE_TO_MAC:
                    raise MediaProtocolError("uplink acknowledgement has the wrong direction")
                if frame.call_id != self.session.call_id:
                    raise MediaProtocolError("uplink acknowledgement belongs to another call")
                body = frame.json_payload()
                if body.get("type") != "audio.playout.ack" or body.get("status") != "ok":
                    raise MediaProtocolError("uplink returned an invalid playout acknowledgement")
                with self._uplink_credit:
                    identity = (frame.generation_id, frame.sequence)
                    if frame.generation_id != self._uplink_credit_generation:
                        # A flush can leave already-written acknowledgements in
                        # the socket. They must never credit the new generation.
                        continue
                    if not self._uplink_pending or self._uplink_pending[0] != identity:
                        raise MediaProtocolError(
                            "uplink playout acknowledgements are missing or out of order"
                        )
                    self._uplink_pending.popleft()
                    send_time = self._uplink_timestamps.pop(identity, None)
                    if send_time is not None:
                        measured_rtt = time.monotonic() - send_time
                        if 0.001 < measured_rtt < 3.0:
                            self._rtt_ema = 0.85 * self._rtt_ema + 0.15 * measured_rtt
                    self.session.mark_rendered(*identity)
                    self._uplink_credit.notify_all()
        except (OSError, MediaProtocolError, LinkError) as exc:
            if not self._stop.is_set():
                self._mark_disconnected(exc)

    def _send_uplink_frame(self, frame: MediaFrame, failure_message: str) -> None:
        """Keep at most one Android playout queue of unrendered speech in flight."""

        channel = self._require_channel("uplink")
        identity = (frame.generation_id, frame.sequence)
        try:
            # The lock makes reservation order identical to wire order. The
            # acknowledgement reader does not use this lock, so it can release
            # credits while a producer waits here.
            with self._tx_lock:
                with self._uplink_credit:
                    ack_deadline = time.monotonic() + self.UPLINK_ACK_TIMEOUT_SECS
                    while len(self._uplink_pending) >= self.UPLINK_WINDOW_FRAMES:
                        if frame.generation_id != self.session.generation_id:
                            raise LinkRejected("refusing cancelled audio generation")
                        if not self.media_connected or self._stop.is_set():
                            raise LinkDisconnected("uplink closed while waiting for playout")
                        remaining = ack_deadline - time.monotonic()
                        if remaining <= 0:
                            raise LinkDisconnected(
                                "phone playout acknowledgements stalled for "
                                f"{self.UPLINK_ACK_TIMEOUT_SECS:g} seconds"
                            )
                        self._uplink_credit.wait(timeout=min(0.1, remaining))
                    if frame.generation_id != self.session.generation_id:
                        raise LinkRejected("refusing cancelled audio generation")
                    if frame.generation_id != self._uplink_credit_generation:
                        raise LinkRejected("phone playout generation is not synchronized")
                    self._uplink_pending.append(identity)
                    self._uplink_timestamps[identity] = time.monotonic()
                try:
                    channel.sock.sendall(
                        encode_frame(frame, authentication_key=self.authentication_key)
                    )
                except OSError:
                    with self._uplink_credit:
                        try:
                            self._uplink_pending.remove(identity)
                            self._uplink_timestamps.pop(identity, None)
                        except ValueError:
                            pass
                        self._uplink_credit.notify_all()
                    raise
        except (OSError, LinkDisconnected) as exc:
            self._mark_disconnected(exc)
            raise LinkDisconnected(failure_message) from exc

    def _reset_uplink_credit(self, generation_id: int) -> None:
        with self._uplink_credit:
            self._uplink_credit_generation = generation_id
            self._uplink_pending.clear()
            self._uplink_timestamps.clear()
            self._uplink_credit.notify_all()

    def _receive_one(self, channel: _Channel) -> MediaFrame:
        if channel.pending:
            return channel.pending.popleft()
        while True:
            chunk = channel.sock.recv(64 * 1024)
            if not chunk:
                raise LinkDisconnected(f"{channel.name} peer closed the connection")
            frames = channel.decoder.feed(chunk)
            if frames:
                channel.pending.extend(frames[1:])
                return frames[0]

    def _require_channel(self, name: str) -> _Channel:
        with self._state_lock:
            channel = self._channels.get(name)
        if channel is None:
            raise LinkDisconnected(f"{name} channel is not connected")
        return channel

    def _mark_disconnected(self, reason: BaseException) -> None:
        logger.warning("authenticated phone link disconnected: %s", reason)
        self._control_connected.clear()
        self._media_connected.clear()
        self._disconnected.set()
        with self._uplink_credit:
            self._uplink_credit.notify_all()
        self._close_channels()

    def _close_channels(self) -> None:
        self._control_connected.clear()
        self._media_connected.clear()
        with self._state_lock:
            channels = tuple(self._channels.values())
            self._channels.clear()
        for channel in channels:
            self._close_socket(channel.sock)
        with self._uplink_credit:
            self._uplink_pending.clear()
            self._uplink_credit.notify_all()
        rx_thread = self._rx_thread
        if rx_thread and rx_thread.is_alive() and rx_thread is not threading.current_thread():
            rx_thread.join(timeout=1.0)
        self._rx_thread = None
        ack_thread = self._uplink_ack_thread
        if ack_thread and ack_thread.is_alive() and ack_thread is not threading.current_thread():
            ack_thread.join(timeout=1.0)
        self._uplink_ack_thread = None

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def load_link_key(path: str) -> bytes:
    """Read a raw private link key without accepting textual ambiguity."""

    try:
        with open(path, "rb") as key_file:
            key = key_file.read(4097)
    except OSError as exc:
        raise LinkError(f"could not read link key: {exc}") from exc
    if len(key) < 32:
        raise LinkError("link key must contain at least 32 bytes")
    if len(key) > 4096:
        raise LinkError("link key exceeds the hard size limit")
    return key


def format_link_error(error: BaseException) -> str:
    """Return a log-safe error string with no frame payload or key material."""

    return json.dumps({"error": type(error).__name__, "message": str(error)})
