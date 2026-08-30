"""Versioned binary framing for PhoneAgent control and PCM media.

The currently deployed Android gateway still exposes raw feasibility sockets.
This module is the production wire contract that both sides will migrate to.
It is deliberately independent of Pipecat so it can be fuzzed and reused by
Android/native implementations.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Any
from uuid import UUID

MAGIC = b"PHAG"
PROTOCOL_VERSION = 1
AUTH_TAG_BYTES = hashlib.sha256().digest_size
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_BUFFER_BYTES = MAX_PAYLOAD_BYTES * 2

# magic, version, kind, direction, flags, call UUID, generation, sequence,
# monotonic timestamp, sample rate, channels, sample width, payload length.
HEADER = struct.Struct("!4sBBBB16sQQQIBBI")


class FrameKind(IntEnum):
    AUDIO = 1
    CONTROL = 2
    ACK = 3
    ERROR = 4
    METRICS = 5


class FrameDirection(IntEnum):
    PHONE_TO_MAC = 1
    MAC_TO_PHONE = 2
    BIDIRECTIONAL = 3


class FrameFlags(IntFlag):
    NONE = 0
    AUTHENTICATED = 1 << 0
    URGENT = 1 << 1
    END_OF_STREAM = 1 << 2


class MediaProtocolError(ValueError):
    """Base protocol validation error."""


class AuthenticationError(MediaProtocolError):
    """Frame authentication was required or failed."""


class FrameTooLarge(MediaProtocolError):
    """Frame exceeds the configured protocol bound."""


@dataclass(frozen=True, slots=True)
class MediaFrame:
    """One authenticated protocol unit.

    Audio uses PCM metadata. Non-audio frames set the audio metadata to zero
    and normally carry compact UTF-8 JSON.
    """

    kind: FrameKind
    direction: FrameDirection
    call_id: UUID
    generation_id: int
    sequence: int
    monotonic_ns: int
    payload: bytes
    sample_rate: int = 0
    channels: int = 0
    sample_width: int = 0
    flags: FrameFlags = FrameFlags.NONE
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.version != PROTOCOL_VERSION:
            raise MediaProtocolError(f"unsupported protocol version: {self.version}")
        if self.generation_id < 1:
            raise MediaProtocolError("generation_id must be >= 1")
        if self.sequence < 0 or self.monotonic_ns < 0:
            raise MediaProtocolError("sequence and monotonic_ns must be non-negative")
        if len(self.payload) > MAX_PAYLOAD_BYTES:
            raise FrameTooLarge(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        if self.kind is FrameKind.AUDIO:
            if self.sample_rate <= 0:
                raise MediaProtocolError("audio sample_rate must be positive")
            if self.channels not in (1, 2):
                raise MediaProtocolError("audio channels must be 1 or 2")
            if self.sample_width not in (1, 2, 4):
                raise MediaProtocolError("unsupported audio sample width")
            frame_width = self.channels * self.sample_width
            if len(self.payload) % frame_width:
                raise MediaProtocolError("audio payload is not sample aligned")
        elif self.sample_rate or self.channels or self.sample_width:
            raise MediaProtocolError("non-audio frames must not carry PCM metadata")

    @classmethod
    def control(
        cls,
        *,
        direction: FrameDirection,
        call_id: UUID,
        generation_id: int,
        sequence: int,
        monotonic_ns: int,
        message: dict[str, Any],
        urgent: bool = False,
    ) -> MediaFrame:
        flags = FrameFlags.URGENT if urgent else FrameFlags.NONE
        payload = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return cls(
            kind=FrameKind.CONTROL,
            direction=direction,
            call_id=call_id,
            generation_id=generation_id,
            sequence=sequence,
            monotonic_ns=monotonic_ns,
            payload=payload,
            flags=flags,
        )

    def json_payload(self) -> dict[str, Any]:
        if self.kind is FrameKind.AUDIO:
            raise MediaProtocolError("audio payload is not JSON")
        try:
            value = json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaProtocolError("invalid JSON payload") from exc
        if not isinstance(value, dict):
            raise MediaProtocolError("control payload must be a JSON object")
        return value


def encode_frame(frame: MediaFrame, *, authentication_key: bytes | None) -> bytes:
    """Encode one frame and optionally append an HMAC-SHA256 tag."""

    if authentication_key is not None and len(authentication_key) < 32:
        raise AuthenticationError("authentication key must contain at least 32 bytes")

    flags = FrameFlags(frame.flags)
    if authentication_key is None:
        flags &= ~FrameFlags.AUTHENTICATED
    else:
        flags |= FrameFlags.AUTHENTICATED

    header = HEADER.pack(
        MAGIC,
        frame.version,
        int(frame.kind),
        int(frame.direction),
        int(flags),
        frame.call_id.bytes,
        frame.generation_id,
        frame.sequence,
        frame.monotonic_ns,
        frame.sample_rate,
        frame.channels,
        frame.sample_width,
        len(frame.payload),
    )
    encoded = header + frame.payload
    if authentication_key is not None:
        encoded += hmac.new(authentication_key, encoded, hashlib.sha256).digest()
    return encoded


def decode_frame(
    data: bytes,
    *,
    authentication_key: bytes | None,
    require_authenticated: bool = True,
) -> MediaFrame:
    """Decode exactly one complete frame and reject trailing bytes."""

    if len(data) < HEADER.size:
        raise MediaProtocolError("incomplete frame header")

    unpacked = HEADER.unpack_from(data)
    magic, version, kind_raw, direction_raw, flags_raw = unpacked[:5]
    call_id_raw, generation_id, sequence, monotonic_ns = unpacked[5:9]
    sample_rate, channels, sample_width, payload_length = unpacked[9:]

    if magic != MAGIC:
        raise MediaProtocolError("invalid frame magic")
    if version != PROTOCOL_VERSION:
        raise MediaProtocolError(f"unsupported protocol version: {version}")
    if payload_length > MAX_PAYLOAD_BYTES:
        raise FrameTooLarge(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")

    flags = FrameFlags(flags_raw)
    authenticated = bool(flags & FrameFlags.AUTHENTICATED)
    if require_authenticated and not authenticated:
        raise AuthenticationError("unauthenticated frame rejected")
    if authenticated and authentication_key is None:
        raise AuthenticationError("no authentication key configured")
    if authentication_key is not None and len(authentication_key) < 32:
        raise AuthenticationError("authentication key must contain at least 32 bytes")

    tag_length = AUTH_TAG_BYTES if authenticated else 0
    expected_length = HEADER.size + payload_length + tag_length
    if len(data) != expected_length:
        raise MediaProtocolError(
            f"frame length mismatch: expected {expected_length}, received {len(data)}"
        )

    payload_end = HEADER.size + payload_length
    if authenticated:
        expected_tag = hmac.new(authentication_key, data[:payload_end], hashlib.sha256).digest()
        if not hmac.compare_digest(expected_tag, data[payload_end:]):
            raise AuthenticationError("frame authentication failed")

    try:
        kind = FrameKind(kind_raw)
        direction = FrameDirection(direction_raw)
    except ValueError as exc:
        raise MediaProtocolError("unknown frame kind or direction") from exc

    return MediaFrame(
        version=version,
        kind=kind,
        direction=direction,
        flags=flags,
        call_id=UUID(bytes=call_id_raw),
        generation_id=generation_id,
        sequence=sequence,
        monotonic_ns=monotonic_ns,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        payload=data[HEADER.size:payload_end],
    )


class FrameStreamDecoder:
    """Incrementally decode fragmented or coalesced TCP frames."""

    def __init__(
        self,
        *,
        authentication_key: bytes | None,
        require_authenticated: bool = True,
    ) -> None:
        self._authentication_key = authentication_key
        self._require_authenticated = require_authenticated
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[MediaFrame]:
        if data:
            self._buffer.extend(data)
        if len(self._buffer) > MAX_BUFFER_BYTES:
            self._buffer.clear()
            raise FrameTooLarge("decoder buffer exceeded its hard bound")

        frames: list[MediaFrame] = []
        while len(self._buffer) >= HEADER.size:
            if self._buffer[: len(MAGIC)] != MAGIC:
                self._buffer.clear()
                raise MediaProtocolError("stream is not aligned to frame magic")

            unpacked = HEADER.unpack_from(self._buffer)
            payload_length = unpacked[-1]
            if payload_length > MAX_PAYLOAD_BYTES:
                self._buffer.clear()
                raise FrameTooLarge(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")

            flags = FrameFlags(unpacked[4])
            tag_length = AUTH_TAG_BYTES if flags & FrameFlags.AUTHENTICATED else 0
            frame_length = HEADER.size + payload_length + tag_length
            if len(self._buffer) < frame_length:
                break

            encoded = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            frames.append(
                decode_frame(
                    encoded,
                    authentication_key=self._authentication_key,
                    require_authenticated=self._require_authenticated,
                )
            )
        return frames

