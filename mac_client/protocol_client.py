"""High-level telephony control over the authenticated PHAG v1 channel."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from phone_agent_gateway.ai_bridge.session import CallSessionState, GenerationAdvance

from .framed_link import FramedGatewayLink, LinkPorts
from .gateway_client import CallState, CallStatus

logger = logging.getLogger("AuthenticatedPhoneAgentClient")


class AuthenticatedPhoneAgentClient:
    """Idempotent call control and state polling for one isolated call session."""

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
        self.session = session
        self.link = FramedGatewayLink(
            session,
            authentication_key,
            host=host,
            ports=ports,
            remote_ports=remote_ports,
            device_id=device_id,
            auto_forward_adb=auto_forward_adb,
        )
        self._listeners: list[Callable[[CallStatus], None]] = []
        self._last_status: CallStatus | None = None
        self._stop_polling = threading.Event()
        self._polling_thread: threading.Thread | None = None

    def connect_control(self) -> None:
        self.link.connect_control()

    def connect_media(self) -> None:
        self.link.connect_media()

    def reconnect(self) -> None:
        """Recover the same authenticated call and media session in place."""

        self.link.reconnect()

    def get_status(self) -> CallStatus:
        return CallStatus.from_dict(self.link.request("call.status"))

    def get_health(self) -> dict:
        return self.link.request("gateway.health")

    def get_audio_status(self) -> dict:
        return self.link.request("audio.status")

    def dial(self, number: str) -> dict:
        clean_number = str(number).strip().replace(" ", "").replace("-", "")
        if clean_number.startswith("00212"):
            clean_number = "0" + clean_number[5:]
        elif clean_number.startswith("+212"):
            clean_number = "0" + clean_number[4:]
        return self.link.request("call.dial", {"number": clean_number})

    def answer(self) -> dict:
        return self.link.request("call.answer")

    def reject(self) -> dict:
        return self.link.request("call.reject")

    def hangup(self) -> dict:
        return self.link.request("call.hangup", urgent=True)

    def send_dtmf(self, digit: str) -> dict:
        return self.link.request("dtmf.send", {"digit": str(digit)})

    def flush_audio(self, advance: GenerationAdvance) -> dict:
        return self.link.flush_audio(advance)

    def add_call_listener(self, callback: Callable[[CallStatus], None]) -> None:
        self._listeners.append(callback)
        if self._polling_thread is None or not self._polling_thread.is_alive():
            self._stop_polling.clear()
            self._polling_thread = threading.Thread(
                target=self._poll_loop,
                name="phoneagent-authenticated-status",
                daemon=True,
            )
            self._polling_thread.start()

    def _poll_loop(self) -> None:
        while not self._stop_polling.is_set():
            try:
                status = self.get_status()
                if self._last_status is None or self._last_status.state != status.state:
                    self._last_status = status
                    for listener in tuple(self._listeners):
                        try:
                            listener(status)
                        except Exception:
                            logger.exception("call status listener failed")
            except Exception as exc:
                logger.debug("authenticated status poll failed: %s", exc)
            self._stop_polling.wait(0.2)

    def close(self) -> None:
        self._stop_polling.set()
        thread = self._polling_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._polling_thread = None
        self.link.close()


def wait_for_state(
    client: AuthenticatedPhoneAgentClient,
    expected: set[CallState],
    *,
    timeout: float,
) -> CallStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get_status()
        if status.state in expected:
            return status
        time.sleep(0.1)
    raise TimeoutError(f"phone did not reach one of {sorted(state.value for state in expected)}")
