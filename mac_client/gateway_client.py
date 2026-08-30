"""PhoneAgent Mac Client SDK.

Communicates with the rooted Android Telephony & Audio Gateway
over USB via ADB port forwarding.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger("PhoneAgentClient")


class CallState(StrEnum):
    IDLE = "IDLE"
    RINGING = "RINGING"
    NEW = "NEW"
    DIALING = "DIALING"
    CONNECTING = "CONNECTING"
    ACTIVE = "ACTIVE"
    HOLDING = "HOLDING"
    DISCONNECTED = "DISCONNECTED"
    SELECT_PHONE_ACCOUNT = "SELECT_PHONE_ACCOUNT"
    UNKNOWN = "UNKNOWN"


@dataclass
class CallStatus:
    status: str
    state: CallState
    state_code: int
    incoming_number: str

    @classmethod
    def from_dict(cls, data: dict) -> CallStatus:
        raw_state = data.get("state", "UNKNOWN")
        try:
            state = CallState(raw_state)
        except ValueError:
            state = CallState.UNKNOWN
        return cls(
            status=data.get("status", "error"),
            state=state,
            state_code=data.get("state_code", 0),
            incoming_number=data.get("incoming_number", ""),
        )


class PhoneAgentClient:
    """Python Client to control the PhoneAgent Gateway over USB."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        auto_forward_adb: bool = True,
        device_id: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.device_id = device_id
        self._polling_thread: threading.Thread | None = None
        self._stop_polling = threading.Event()
        self._listeners: list[Callable[[CallStatus], None]] = []
        self._last_status: CallStatus | None = None

        if auto_forward_adb:
            self.ensure_adb_forward()

    def ensure_adb_forward(self) -> None:
        """Ensure legacy diagnostics and production protocol ports are forwarded."""
        for port in (self.port, 8766, 8767, 8768):
            cmd = ["adb"]
            if self.device_id:
                cmd.extend(["-s", self.device_id])
            cmd.extend(["forward", f"tcp:{port}", f"tcp:{port}"])
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                logger.info("ADB port forward established for %d", port)
            except Exception as exc:
                logger.warning("Failed to auto-forward ADB port %d: %s", port, exc)

    def _request(self, endpoint: str, data: dict | None = None) -> dict:
        """Sends an HTTP request to the Phone Gateway."""
        url = f"{self.base_url}{endpoint}"
        req_data = None
        headers = {"User-Agent": "PhoneAgentClient/1.0"}

        if data is not None and len(data) > 0:
            query = urllib.parse.urlencode(data)
            url = f"{url}?{query}"
            req_data = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif data is not None:
            req_data = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=req_data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                error = json.loads(body)
            except json.JSONDecodeError:
                error = {"status": "error", "message": body or str(exc)}
            error["http_status"] = exc.code
            return error
        except urllib.error.URLError as exc:
            logger.error("Request to %s failed: %s", url, exc)
            raise ConnectionError(f"Cannot connect to PhoneAgent Gateway at {url}: {exc}") from exc

    def get_status(self) -> CallStatus:
        """Fetches the live telephony call status from the phone."""
        res = self._request("/call/status")
        return CallStatus.from_dict(res)

    def dial(self, number: str) -> dict:
        """Places an outbound cellular phone call."""
        clean_num = str(number).strip().replace(" ", "").replace("-", "")
        if clean_num.startswith("00212"):
            clean_num = "0" + clean_num[5:]
        elif clean_num.startswith("+212"):
            clean_num = "0" + clean_num[4:]

        logger.info("Placing outbound call to %s", clean_num)
        return self._request("/call/dial", {"number": clean_num})

    def answer(self) -> dict:
        """Answers an active incoming phone call."""
        logger.info("Answering incoming call")
        return self._request("/call/answer", {})

    def reject(self) -> dict:
        """Rejects an active incoming phone call."""
        logger.info("Rejecting incoming call")
        return self._request("/call/reject", {})

    def hangup(self) -> dict:
        """Terminates the active phone call."""
        logger.info("Hanging up call")
        return self._request("/call/hangup", {})

    def send_dtmf(self, digit: str) -> dict:
        """Sends a keypad DTMF tone (0-9, *, #)."""
        logger.info("Sending DTMF digit: %s", digit)
        return self._request("/call/dtmf", {"digit": str(digit)})

    def get_health(self) -> dict:
        """Returns gateway, dialer-role, call, and audio readiness."""
        return self._request("/health")

    def get_audio_status(self) -> dict:
        """Returns the phone-side telephony audio diagnostic status."""
        return self._request("/audio/status")

    def flush_audio(self) -> dict:
        """Flushes provisional phone-side uplink playback and advances generation."""
        return self._request("/audio/flush", {})

    def add_call_listener(self, callback: Callable[[CallStatus], None]) -> None:
        """Registers a callback for call state changes."""
        self._listeners.append(callback)
        if self._polling_thread is None or not self._polling_thread.is_alive():
            self._start_polling()

    def _start_polling(self) -> None:
        self._stop_polling.clear()
        self._polling_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._polling_thread.start()

    def _poll_loop(self) -> None:
        while not self._stop_polling.is_set():
            try:
                status = self.get_status()
                if self._last_status is None or self._last_status.state != status.state:
                    self._last_status = status
                    for listener in self._listeners:
                        try:
                            listener(status)
                        except Exception as exc:
                            logger.exception("Error in call status listener: %s", exc)
            except Exception as exc:
                logger.debug("Polling status check failed: %s", exc)
            time.sleep(0.5)

    def close(self) -> None:
        """Stops listeners and cleans up connections."""
        self._stop_polling.set()
        if self._polling_thread and self._polling_thread.is_alive():
            self._polling_thread.join(timeout=1)
