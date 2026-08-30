"""A WhatsApp call wearing the same shape as the cellular gateway client.

``PhoneVoiceAgent`` drives a call through a small, stable surface: connect,
dial, answer, status, flush, hang up, close — plus a ``link`` exposing the four
audio callbacks. This presents that surface over WhatsApp so the agent can pick
a channel without the cellular code learning anything about it.

Nothing here touches the cellular path. There is no adb, no framed TCP link, no
device handle: only a local subprocess and a socket to WhatsApp. One call runs
at a time, enforced by the existing voice-host lock, so choosing this channel
cannot disturb a GSM call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..mac_client.gateway_client import CallState, CallStatus
from .session import CallSessionState, SessionPhase
from .whatsapp_link import WhatsAppLink, WhatsAppLinkError

logger = logging.getLogger("WhatsAppClient")


class WhatsAppPhoneClient:
    """One outbound WhatsApp call, driven like the cellular client."""

    def __init__(
        self,
        session: CallSessionState,
        *,
        country_code: str = "212",
        binary: str | None = None,
        max_duration_secs: int = 900,
    ) -> None:
        self.session = session
        # Captured here because the client is constructed on the pipeline's loop.
        # The audio pumps, the subprocess and the queues must all live on that
        # loop: created on a throwaway one they would die the moment dial()
        # returned, leaving a connected call with no audio in either direction.
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self.link = WhatsAppLink(
            binary=binary,
            country_code=country_code,
            max_duration_secs=max_duration_secs,
            render_ack_handler=session.mark_rendered,
        )
        self._number = ""
        self._state = CallState.IDLE

    # -- lifecycle the agent calls --------------------------------------------

    def connect_control(self) -> None:
        """No control channel to open: the subprocess is started by dial()."""

    def connect_media(self) -> None:
        """Media rides the same subprocess as control."""

    def reconnect(self) -> None:
        """A dropped WhatsApp call cannot be resumed; it has to be redialled."""

        raise WhatsAppLinkError("a WhatsApp call cannot be reconnected; dial again")

    def dial(self, number: str) -> dict[str, Any]:
        """Place the call. Blocking, to match the cellular client's shape."""

        self._number = number
        self._state = CallState.DIALING
        self.session.set_phase(SessionPhase.CONNECTING)
        try:
            self._call(self.link.dial(number))
        except WhatsAppLinkError:
            self._state = CallState.IDLE
            raise
        self._state = CallState.ACTIVE
        self.session.set_phase(SessionPhase.ACTIVE)
        logger.info("WhatsApp call answered by %s", number)
        return {"status": "ok", "state": self._state.value, "number": number}

    def answer(self) -> dict[str, Any]:
        """Inbound WhatsApp calls are not handled; this channel dials out only."""

        raise WhatsAppLinkError("the WhatsApp channel does not accept inbound calls")

    def get_status(self) -> CallStatus:
        """The agent's own status type, so nothing downstream special-cases us."""

        if self.link.ended and self._state is CallState.ACTIVE:
            self._state = CallState.IDLE
        return CallStatus(
            status="ok",
            state=self._state,
            state_code=0,
            incoming_number=self._number,
        )

    def get_audio_status(self) -> dict[str, Any]:
        """WhatsApp gives no playout acknowledgement, so there is nothing to
        report. The cellular counters exist because Android's AudioTrack is the
        playout clock; here the peer's device is, and it never tells us."""

        return {
            "channel": "whatsapp",
            "acknowledged_playout": False,
            "transport_acknowledged": True,
            "ack_semantics": "accepted_by_whatsapp_rust_media_source",
        }

    def flush_audio(self, advance_generation: bool = True) -> dict[str, Any]:
        return self.link.flush_audio(advance_generation)

    def hangup(self) -> dict[str, Any]:
        self._call(self.link.hangup())
        self._state = CallState.IDLE
        return {"status": "ok"}

    def _call(self, coroutine: Any) -> Any:
        """Run a coroutine on the pipeline's loop, from any thread.

        The agent invokes dial and hangup through asyncio.to_thread, so this is
        usually called off-loop. Everything the call owns has to be created on
        the loop that outlives the request.
        """

        loop = self._loop
        if loop is None or loop.is_closed():
            return asyncio.run(coroutine)
        try:
            if asyncio.get_running_loop() is loop:
                raise RuntimeError("_call must not be used from the loop it targets")
        except RuntimeError as exc:
            if "must not be used" in str(exc):
                raise
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    def close(self) -> None:
        try:
            self._call(self.link.hangup())
        except Exception:
            logger.debug("WhatsApp hangup during close failed", exc_info=True)
        self._state = CallState.IDLE


