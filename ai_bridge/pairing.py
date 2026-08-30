"""Pair a handset with a runtime by showing it a QR code.

The shared link key authenticates both the USB media protocol and the remote
tunnel, so the two sides disagreeing breaks everything at once and does so
silently: the phone simply never connects. Moving the key by hand -- reading it
out of a file, pasting it into a chat, typing it on a phone -- is what made that
happen, and a typed key is a weak key besides.

Studio therefore generates the pairing material and renders it as a QR code.
One scan carries the key, the address and the port together, so the handset
cannot end up correctly keyed but pointed at the wrong host, or vice versa.

The payload is deliberately short. A QR holding a 32-byte key, a hostname and a
port stays inside a version the phone camera reads in one pass at arm's length.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LINK_KEY_BYTES = 32
PAIRING_SCHEME = "phoneagent-pair"
PAIRING_VERSION = 1


def link_key_path() -> Path:
    configured = os.getenv("PHONE_AGENT_LINK_KEY_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "phone-agent" / "link.key"


def read_link_key() -> bytes | None:
    try:
        data = link_key_path().read_bytes()
    except OSError:
        return None
    return data if len(data) >= 16 else None


def write_link_key(key: bytes) -> None:
    """Store the key readable only by this account.

    Written to a temporary file first: a half-written key would authenticate
    nothing and leave both the tunnel and the USB path broken with no obvious
    cause.
    """

    if len(key) < 16:
        raise ValueError("a link key must be at least 16 bytes")
    path = link_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(key)
    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(path)


def generate_link_key() -> bytes:
    return secrets.token_bytes(LINK_KEY_BYTES)


def key_fingerprint(key: bytes) -> str:
    """A short, comparable identity for a key that is never itself displayed.

    Studio and the handset both show this, so an operator can confirm the two
    sides match without either revealing the secret.
    """

    return hashlib.sha256(key).hexdigest()[:12].upper()


@dataclass(slots=True)
class PairingPayload:
    key: bytes
    host: str
    port: int

    def to_uri(self) -> str:
        """The exact text encoded in the QR."""

        body = {
            "v": PAIRING_VERSION,
            "k": base64.urlsafe_b64encode(self.key).decode().rstrip("="),
            "h": self.host,
            "p": self.port,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(body, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        return f"{PAIRING_SCHEME}:{encoded}"

    @classmethod
    def from_uri(cls, text: str) -> PairingPayload:
        value = text.strip()
        if not value.startswith(PAIRING_SCHEME + ":"):
            raise ValueError("not a PhoneAgent pairing code")
        encoded = value[len(PAIRING_SCHEME) + 1 :]
        padded = encoded + "=" * (-len(encoded) % 4)
        try:
            body = json.loads(base64.urlsafe_b64decode(padded).decode())
        except Exception as exc:
            raise ValueError("pairing code is not readable") from exc
        if body.get("v") != PAIRING_VERSION:
            raise ValueError(f"unsupported pairing version {body.get('v')}")
        raw = str(body.get("k", ""))
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(key) < 16:
            raise ValueError("pairing code carried too short a key")
        host = str(body.get("h", "")).strip()
        if not host:
            raise ValueError("pairing code carried no address")
        port = int(body.get("p", 0))
        if not 1 <= port <= 65535:
            raise ValueError("pairing code carried an invalid port")
        return cls(key=key, host=host, port=port)

    def to_qr_svg(self, scale: int = 6) -> str:
        """Render the code as inline SVG.

        SVG rather than a bitmap so it stays sharp at any size on screen, which
        is what a phone camera actually needs to read it in one pass.
        """

        import io

        import segno

        # Error correction M: enough to survive screen glare and a camera at an
        # angle without pushing the code into a denser version.
        qr = segno.make(self.to_uri(), error="m")
        buffer = io.BytesIO()
        qr.save(buffer, kind="svg", scale=scale, border=2, xmldecl=False, svgns=True)
        return buffer.getvalue().decode("utf-8")


def build_pairing(host: str, port: int, *, rotate: bool = False) -> PairingPayload:
    """Pairing material for the handset, reusing the current key by default.

    Rotating invalidates the USB path until the phone is paired again, so it is
    never the default: an operator who only wants to attach a phone should not
    silently break the cable they already have working.
    """

    key = None if rotate else read_link_key()
    if key is None:
        key = generate_link_key()
        write_link_key(key)
    return PairingPayload(key=key, host=host, port=port)


def pairing_status(host: str, port: int) -> dict[str, Any]:
    key = read_link_key()
    return {
        "has_key": key is not None,
        "fingerprint": key_fingerprint(key) if key else "",
        "host": host,
        "port": port,
    }
