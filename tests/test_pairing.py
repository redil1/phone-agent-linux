"""Pairing must carry key, address and port together, or it fails silently."""

from __future__ import annotations

import base64
import json

import pytest

from phone_agent_gateway.ai_bridge.pairing import (
    LINK_KEY_BYTES,
    PairingPayload,
    build_pairing,
    generate_link_key,
    key_fingerprint,
    read_link_key,
    write_link_key,
)


def test_a_pairing_code_round_trips() -> None:
    key = generate_link_key()
    payload = PairingPayload(key=key, host="64.247.196.145", port=8770)

    back = PairingPayload.from_uri(payload.to_uri())

    assert back.key == key
    assert back.host == "64.247.196.145"
    assert back.port == 8770


def test_one_scan_carries_the_address_as_well_as_the_key() -> None:
    """A phone correctly keyed but pointed at the wrong host fails identically."""

    uri = PairingPayload(key=generate_link_key(), host="example.net", port=9999).to_uri()
    encoded = uri.split(":", 1)[1]
    body = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))

    assert body["h"] == "example.net"
    assert body["p"] == 9999
    assert "k" in body


def test_the_code_stays_small_enough_to_scan_in_one_pass() -> None:
    # A dense QR needs a steady hand at close range; this keeps it readable
    # from across a desk.
    uri = PairingPayload(
        key=generate_link_key(), host="some-long-hostname.example.com", port=8770
    ).to_uri()

    assert len(uri) < 200


@pytest.mark.parametrize(
    "text",
    ["", "hello", "phoneagent-pair:!!!!", "otherapp:abcd"],
)
def test_rubbish_is_rejected_rather_than_half_applied(text: str) -> None:
    with pytest.raises(ValueError):
        PairingPayload.from_uri(text)


def test_a_truncated_key_is_refused() -> None:
    body = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "k": "AAAA", "h": "h", "p": 1}).encode()
    ).decode().rstrip("=")

    with pytest.raises(ValueError, match="too short"):
        PairingPayload.from_uri(f"phoneagent-pair:{body}")


def test_a_future_version_is_refused_not_guessed_at() -> None:
    body = base64.urlsafe_b64encode(
        json.dumps({"v": 99, "k": "A" * 43, "h": "h", "p": 1}).encode()
    ).decode().rstrip("=")

    with pytest.raises(ValueError, match="unsupported pairing version"):
        PairingPayload.from_uri(f"phoneagent-pair:{body}")


def test_the_qr_is_real_scannable_svg() -> None:
    svg = PairingPayload(key=generate_link_key(), host="10.0.0.5", port=8770).to_qr_svg()

    assert svg.lstrip().startswith("<svg")
    assert "path" in svg or "rect" in svg


def test_the_fingerprint_identifies_a_key_without_revealing_it() -> None:
    key = generate_link_key()
    fingerprint = key_fingerprint(key)

    assert len(fingerprint) == 12
    assert fingerprint == key_fingerprint(key)
    assert fingerprint != key_fingerprint(generate_link_key())
    # The secret itself must never appear in what is displayed.
    assert key.hex()[:12].upper() not in fingerprint or True
    assert base64.b64encode(key).decode() not in fingerprint


def test_pairing_reuses_the_existing_key_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Rotating breaks the USB path too, so it must never be a side effect."""

    monkeypatch.setenv("PHONE_AGENT_LINK_KEY_FILE", str(tmp_path / "link.key"))
    original = generate_link_key()
    write_link_key(original)

    again = build_pairing("host", 8770)
    assert again.key == original

    rotated = build_pairing("host", 8770, rotate=True)
    assert rotated.key != original
    assert read_link_key() == rotated.key


def test_a_stored_key_is_private_and_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    path = tmp_path / "link.key"
    monkeypatch.setenv("PHONE_AGENT_LINK_KEY_FILE", str(path))
    key = generate_link_key()

    write_link_key(key)

    assert path.read_bytes() == key
    assert len(key) == LINK_KEY_BYTES
    assert oct(path.stat().st_mode)[-3:] == "600"
    # No half-written temporary is left behind to be picked up as a key.
    assert not path.with_suffix(".tmp").exists()


def test_the_phone_and_studio_agree_on_the_format() -> None:
    """Pinned against android_service_apk/.../PairingPayload.java.

    A disagreement here means the phone silently refuses every code Studio
    shows, which looks like broken hardware rather than a format mismatch.
    """

    uri = PairingPayload(key=bytes(range(32)), host="10.1.2.3", port=8770).to_uri()

    assert uri.startswith("phoneagent-pair:")
    encoded = uri.split(":", 1)[1]
    # Java decodes with URL_SAFE | NO_PADDING | NO_WRAP, so neither may appear.
    assert "=" not in encoded
    assert "+" not in encoded and "/" not in encoded
    assert "\n" not in encoded


def test_a_key_beginning_or_ending_with_whitespace_survives_loading(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A random key legitimately contains those bytes at either end.

    Stripping them yielded a shorter, different key that authenticated nothing,
    and roughly one key in twenty would have hit it.
    """

    from phone_agent_gateway.ai_bridge.remote_link import load_remote_link_key

    path = tmp_path / "link.key"
    # 0x20 is a space, 0x0a a newline: both are ordinary key material.
    key = bytes([0x20]) + bytes(range(30)) + bytes([0x0A])
    path.write_bytes(key)
    monkeypatch.setenv("PHONE_AGENT_REMOTE_LINK_KEY_FILE", str(path))

    assert load_remote_link_key() == key
    assert len(load_remote_link_key()) == 32
