"""Tests for authenticated, incrementally decoded media frames."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from phone_agent_gateway.ai_bridge.media_protocol import (
    AuthenticationError,
    FrameDirection,
    FrameFlags,
    FrameKind,
    FrameStreamDecoder,
    MediaFrame,
    decode_frame,
    encode_frame,
)

KEY = bytes(range(32))
GOLDEN_FRAME_HEX = (
    "504841470101020100112233445566778899aabbccddeeff0000000000000007"
    "0000000000000013000000000001e24000003e8001020000000401020304d348"
    "2ea78cd067be360fb3bb59d378f70fb6c839358fe4e787b02e4709c39154"
)


def frame(payload: bytes = b"audio!") -> MediaFrame:
    return MediaFrame(
        kind=FrameKind.AUDIO,
        direction=FrameDirection.PHONE_TO_MAC,
        call_id=uuid4(),
        generation_id=7,
        sequence=19,
        monotonic_ns=123_456,
        payload=payload,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
    )


def test_authenticated_round_trip() -> None:
    original = frame()
    encoded = encode_frame(original, authentication_key=KEY)
    assert decode_frame(encoded, authentication_key=KEY) == replace(
        original, flags=FrameFlags.AUTHENTICATED
    )


def test_tampering_is_rejected() -> None:
    encoded = bytearray(encode_frame(frame(), authentication_key=KEY))
    encoded[-1] ^= 1
    with pytest.raises(AuthenticationError):
        decode_frame(bytes(encoded), authentication_key=KEY)


def test_stream_decoder_handles_fragmented_and_coalesced_tcp_reads() -> None:
    first = frame(b"aa")
    second = frame(b"b" * 640)
    wire = encode_frame(first, authentication_key=KEY) + encode_frame(
        second, authentication_key=KEY
    )
    decoder = FrameStreamDecoder(authentication_key=KEY)

    decoded = []
    for offset in range(0, len(wire), 37):
        decoded.extend(decoder.feed(wire[offset : offset + 37]))

    assert decoded == [
        replace(first, flags=FrameFlags.AUTHENTICATED),
        replace(second, flags=FrameFlags.AUTHENTICATED),
    ]


def test_java_android_golden_vector_is_stable() -> None:
    golden = MediaFrame(
        kind=FrameKind.AUDIO,
        direction=FrameDirection.MAC_TO_PHONE,
        call_id=UUID("00112233-4455-6677-8899-aabbccddeeff"),
        generation_id=7,
        sequence=19,
        monotonic_ns=123_456,
        payload=bytes.fromhex("01020304"),
        sample_rate=16_000,
        channels=1,
        sample_width=2,
    )
    assert encode_frame(golden, authentication_key=KEY).hex() == GOLDEN_FRAME_HEX
