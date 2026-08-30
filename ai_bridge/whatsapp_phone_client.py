"""A WhatsApp call placed by the Android phone, carried on the cellular audio path.

The direct Mac channel has to decode WhatsApp's own codec, and the available
decoder is dimensioned for 16 kHz MLow while an iPhone peer sends 32 kHz — the
rate appears only in the TOC parser, with no decode path behind it. Letting
WhatsApp on the phone decode its own audio sidesteps the codec entirely.

Only the dial differs from a cellular call. Everything else — the link, the four
audio callbacks, flush, hangup — is delegated to the cellular client, so the GSM
path is reused rather than reimplemented.

The dial deliberately avoids the contacts provider. WhatsApp's own call intent
needs a saved contact carrying a ``voip.call`` data row, which a sales dialer
calling unknown numbers will never have — and on a companion-linked device the
contacts provider can be empty entirely. A ``wa.me`` deep link opens a
conversation with any number, contact or not, and the toolbar there offers the
call directly.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from typing import Any

from ..mac_client.gateway_client import CallState, CallStatus

logger = logging.getLogger("WhatsAppPhoneClient")

WHATSAPP_PACKAGE = "com.whatsapp"
ADB_TIMEOUT_SECS = 20.0
# How long WhatsApp may take to open the conversation and draw its toolbar.
UI_READY_TIMEOUT_SECS = 20.0
UI_POLL_SECS = 1.5

# The toolbar button carries no resource id, only a localized description, so
# the labels WhatsApp uses are matched directly. ContactInfoActivity's button
# does carry an id, which is locale-independent — that is the fallback.
CALL_LABELS = (
    "voice call",
    "appel vocal",
    "llamada de voz",
    "chamada de voz",
    "sprachanruf",
    "مكالمة صوتية",
)
CONTACT_INFO_CALL_ID = "com.whatsapp:id/action_call"
# The framework's own id for a dialog's confirming button — not localized.
CONFIRM_BUTTON_ID = "android:id/button1"
# How long WhatsApp may take to raise its "Start voice call?" prompt.
CONFIRM_TIMEOUT_SECS = 8.0
# How long WhatsApp may take to actually bring the call up once confirmed.
CALL_START_TIMEOUT_SECS = 25.0
UI_DUMP_PATH = "/sdcard/pa_ui.xml"
# Long enough for the keyguard animation to finish before the first dump.
WAKE_SETTLE_SECS = 2.0
# The call check is cheap, so it is asked often once the prompt window closes.
CALL_POLL_SECS = 0.3


class WhatsAppPhoneError(RuntimeError):
    """The phone could not place a WhatsApp call."""

    
class WhatsAppPhoneClient:
    """The cellular client, dialling through WhatsApp instead of the dialer."""

    def __init__(
        self,
        inner: Any,
        *,
        device_id: str | None = None,
        country_code: str = "212",
    ) -> None:
        self._inner = inner
        self._device_id = (device_id or "").strip()
        self._country_code = country_code
        self._number = ""
        self._uid: int | None = None

    # Everything except dial is the cellular client, untouched.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_status(self) -> Any:
        """Report the WhatsApp call's state, which Telecom cannot see.

        The agent drives itself off the phone's call state: it waits for ACTIVE
        before bringing media up. A WhatsApp call is never registered with
        Telecom, so delegating here would report IDLE for the whole call and the
        agent would wait forever for a call already in progress.

        The observable equivalent is the audio mode WhatsApp holds: it takes
        MODE_IN_COMMUNICATION when the call connects and drops it when the call
        ends.
        """

        status = self._inner.get_status()
        in_call = self._voip_call_in_progress()
        state = CallState.ACTIVE if in_call else CallState.IDLE
        if state is not status.state:
            logger.debug("WhatsApp call state: %s", state.value)
        return CallStatus(
            status="ok",
            state=state,
            state_code=status.state_code,
            incoming_number=self._number,
        )

    def _voip_call_in_progress(self) -> bool:
        """Whether WhatsApp — specifically — currently holds a call's audio mode.

        The audio mode is global, so testing it alone reports any app's VoIP
        activity as our call. It also reads as in-call while this bridge's own
        injector track is up, which made a dial that never connected look
        answered: the agent brought media up and spoke into a call that did not
        exist. Matching the mode's owning uid against WhatsApp's makes it proof
        of WhatsApp's call and nothing else.
        """

        uid = self._whatsapp_uid()
        if uid is None:
            return False
        try:
            dump = self._adb("shell", "dumpsys", "audio")
        except WhatsAppPhoneError:
            return False
        for line in dump.splitlines():
            if "mAudioModeOwner" not in line:
                continue
            owner = re.search(r"mUid=(\d+)", line)
            return "MODE_IN_COMMUNICATION" in line and owner is not None and (
                owner.group(1) == str(uid)
            )
        return False

    def _whatsapp_uid(self) -> int | None:
        """WhatsApp's uid, cached — it only changes on reinstall."""

        if self._uid is None:
            try:
                listed = self._adb("shell", "pm", "list", "packages", "-U", WHATSAPP_PACKAGE)
            except WhatsAppPhoneError:
                return None
            found = re.search(r"uid:(\d+)", listed)
            self._uid = int(found.group(1)) if found else None
        return self._uid

    def connect_media(self) -> Any:
        """Select the VoIP audio route before media comes up.

        The bridge captures the modem for a cellular call and the system output
        mix for a VoIP one, and the choice has to be made before the uplink
        handshake — the route is proven during it. Cellular remains the service's
        default, so this is the only thing that ever turns it on.
        """

        # The route was selected before dialling, which is what registers the
        # injector mix. Asked again here it is a no-op, and kept only so media
        # cannot come up on the cellular route if dial() is ever bypassed.
        self._select_route("voip")
        return self._inner.connect_media()

    def close(self) -> Any:
        """Return the phone to the cellular route on the way out.

        The injector policy diverts every app's microphone while registered, so
        leaving VoIP mode set would quietly break the next GSM call.
        """

        # Restoring first, while the control link is still up: closing the inner
        # client tears that link down, so a restore afterwards always failed —
        # and the resulting LinkDisconnected propagated out of close() and
        # masked whatever had actually ended the call.
        try:
            self._select_route("cellular")
        except Exception:
            # Never let tidying up replace the real reason the call ended.
            logger.warning("could not restore the cellular audio route", exc_info=True)
        return self._inner.close()

    def _select_route(self, route: str) -> None:
        """Ask the phone for an audio route over the framed control link.

        The authenticated client speaks the framed protocol, not HTTP, so this
        goes through the same link every other call command uses.
        """

        reply = self._inner.link.request("audio.route", {"route": route})
        if reply.get("status") != "ok":
            raise WhatsAppPhoneError(
                f"the phone refused the {route} audio route: "
                f"{reply.get('message', reply)}"
            )
        logger.info("phone audio route set to %s", route)

    def dial(self, number: str) -> dict[str, Any]:
        """Start a WhatsApp call on the phone; audio follows the cellular path."""

        target = self._normalize(number)
        self._number = target
        self._require_whatsapp()
        # Before WhatsApp opens its microphone, not after: Android binds a
        # recording to an audio policy mix when the recorder starts, so the
        # injector has to exist first or the agent is inaudible.
        self._select_route("voip")
        self._wake_screen()
        self._adb(
            "shell", "am", "start",
            "-a", "android.intent.action.VIEW",
            "-d", f"https://wa.me/{target.lstrip('+')}",
        )
        x, y = self._await_call_control()
        logger.info("calling %s over WhatsApp on the phone", target)
        self._adb("shell", "input", "tap", str(x), str(y))
        self._await_call(target)
        return {"status": "ok", "number": target, "via": "whatsapp_phone"}

    def _require_call_started(self, target: str) -> None:
        """Fail unless WhatsApp actually brought a call up.

        Driving a UI can miss: a tap can land on a screen that has already
        changed, and WhatsApp then sits on a chat or a picker instead of
        calling. Returning success there is the worst outcome — the agent
        connects media and holds a full conversation into a call nobody placed,
        which is exactly what happened before this check existed.
        """

        deadline = time.monotonic() + CALL_START_TIMEOUT_SECS
        while time.monotonic() < deadline:
            if self._voip_call_in_progress():
                logger.info("WhatsApp call to %s is up", target)
                return
            time.sleep(UI_POLL_SECS)
        raise WhatsAppPhoneError(
            f"WhatsApp never started a call to {target}. The number may not be on "
            "WhatsApp, or the call screen did not open."
        )

    def _await_call(self, target: str) -> None:
        """Wait for the call, answering WhatsApp's prompt if one appears.

        These were two sequential waits: one for a confirmation dialog, then one
        for the call. When no dialog appeared — a saved contact — the first wait
        still burned its whole timeout before the second began, so media did not
        attach until the caller had already been listening to silence for half a
        minute and hung up. Watching for both at once ends the moment either
        resolves.
        """

        started = time.monotonic()
        deadline = started + CALL_START_TIMEOUT_SECS
        prompt_deadline = started + CONFIRM_TIMEOUT_SECS
        confirmed = False
        while time.monotonic() < deadline:
            # Cheap: a tenth of a second, so it can be asked constantly.
            if self._voip_call_in_progress():
                logger.info(
                    "WhatsApp call to %s is up after %.1fs",
                    target, time.monotonic() - started,
                )
                return
            # Costly: nearly three seconds per dump, so it is only worth doing
            # while a prompt could still be on screen. Polling it for the whole
            # wait would slow the call check down to one look every three
            # seconds, which is most of the delay the caller hears as silence.
            if not confirmed and time.monotonic() < prompt_deadline:
                spot = self._locate_confirm_button(self._ui_dump())
                if spot is not None:
                    logger.info("confirming WhatsApp's call prompt")
                    self._adb("shell", "input", "tap", str(spot[0]), str(spot[1]))
                    confirmed = True
            else:
                time.sleep(CALL_POLL_SECS)
        raise WhatsAppPhoneError(
            f"WhatsApp never started a call to {target}. The number may not be on "
            "WhatsApp, or the call screen did not open."
        )

    def _confirm_if_prompted(self) -> None:
        """Answer WhatsApp's "Start voice call?" prompt, if it appears.

        WhatsApp confirms before calling a number that is not a saved contact —
        which, for a dialer working through a list, is every number. Without this
        the call sits on the dialog forever: the phone shows a prompt nobody
        taps, and the pipeline waits for a call that never goes active.

        A saved contact is dialled without the prompt, so its absence is normal
        rather than a failure.
        """

        deadline = time.monotonic() + CONFIRM_TIMEOUT_SECS
        while time.monotonic() < deadline:
            dump = self._ui_dump()
            spot = self._locate_confirm_button(dump)
            if spot is not None:
                logger.info("confirming WhatsApp's call prompt")
                self._adb("shell", "input", "tap", str(spot[0]), str(spot[1]))
                return
            time.sleep(UI_POLL_SECS)
        logger.info("no call confirmation prompt appeared; the call started directly")

    @staticmethod
    def _locate_confirm_button(dump: str) -> tuple[int, int] | None:
        """The dialog's positive button, found by its platform id.

        ``android:id/button1`` is the framework's own id for the confirming
        button, so this does not depend on the phone's language the way matching
        the word "Call" would.
        """

        for node in dump.split("<node")[1:]:
            if CONFIRM_BUTTON_ID not in node or 'clickable="true"' not in node:
                continue
            bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if bounds is None:
                continue
            left, top, right, bottom = (int(g) for g in bounds.groups())
            return (left + right) // 2, (top + bottom) // 2
        return None

    def _wake_screen(self) -> None:
        """Wake the phone and clear the keyguard before driving WhatsApp.

        A gateway phone sits idle between calls and its screen locks. WhatsApp
        then cannot come to the foreground: the accessibility tree describes the
        lock screen, so the call button is never found — or worse, a tap lands
        on whatever the keyguard has at those coordinates. That is what placed a
        call nobody received while the agent believed it was connected.
        """

        try:
            self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
            self._adb("shell", "wm", "dismiss-keyguard")
        except WhatsAppPhoneError:
            # A locked screen is reported by the call check that follows, which
            # gives a better message than failing here would.
            logger.warning("could not wake the phone before dialling", exc_info=True)
        time.sleep(WAKE_SETTLE_SECS)

    # -- driving WhatsApp's UI --------------------------------------------------

    def _await_call_control(self) -> tuple[int, int]:
        """Wait for the call button to be drawn, and return where to tap it."""

        deadline = time.monotonic() + UI_READY_TIMEOUT_SECS
        last_seen = "nothing yet"
        while time.monotonic() < deadline:
            dump = self._ui_dump()
            if dump:
                found = self._locate_call_control(dump)
                if found is not None:
                    return found
                last_seen = "conversation drawn, no call control"
            time.sleep(UI_POLL_SECS)
        raise WhatsAppPhoneError(
            f"WhatsApp did not present a call button in time ({last_seen}). The number "
            "may not be on WhatsApp."
        )

    def _ui_dump(self) -> str:
        """The screen's accessibility tree, as WhatsApp currently renders it.

        The file is removed first. A dump taken while the screen is mid-
        transition fails and leaves the previous one in place, and reading that
        describes a screen that no longer exists — so the buttons it reports are
        tapped at coordinates that now belong to something else. That is how a
        dial ended up in the contact picker instead of placing a call.
        """

        try:
            dump = self._adb(
                "shell",
                f"rm -f {UI_DUMP_PATH}; "
                f"uiautomator dump {UI_DUMP_PATH} >/dev/null 2>&1; "
                f"cat {UI_DUMP_PATH} 2>/dev/null",
            )
        except WhatsAppPhoneError:
            return ""
        return dump if "<hierarchy" in dump else ""

    def _locate_call_control(self, dump: str) -> tuple[int, int] | None:
        """Centre of the call button, by description or by contact-info id."""

        for node in dump.split("<node")[1:]:
            described = re.search(r'content-desc="([^"]*)"', node)
            resource = re.search(r'resource-id="([^"]*)"', node)
            label = (described.group(1) if described else "").strip().lower()
            identifier = resource.group(1) if resource else ""
            matches = label in CALL_LABELS or identifier == CONTACT_INFO_CALL_ID
            if not matches or 'clickable="true"' not in node:
                continue
            bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if bounds is None:
                continue
            left, top, right, bottom = (int(g) for g in bounds.groups())
            return (left + right) // 2, (top + bottom) // 2
        return None

    # -- adb plumbing ----------------------------------------------------------

    def _adb(self, *args: str) -> str:
        if shutil.which("adb") is None:
            raise WhatsAppPhoneError("adb is not installed; the phone cannot be driven")
        command = ["adb"]
        if self._device_id:
            command += ["-s", self._device_id]
        command += list(args)
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=ADB_TIMEOUT_SECS, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WhatsAppPhoneError(f"adb failed: {exc}") from exc
        if result.returncode != 0:
            raise WhatsAppPhoneError(
                f"adb {' '.join(args[:2])} failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout

    def _require_whatsapp(self) -> None:
        packages = self._adb("shell", "pm", "list", "packages", WHATSAPP_PACKAGE)
        if WHATSAPP_PACKAGE not in packages:
            raise WhatsAppPhoneError(
                "WhatsApp is not installed on the phone. Install it and sign in, then "
                "this channel can place calls through it."
            )

    def _normalize(self, number: str) -> str:
        """International form, which is what a wa.me link expects."""

        digits = re.sub(r"[^\d+]", "", str(number).strip())
        if digits.startswith("+"):
            return digits
        if digits.startswith("00"):
            return "+" + digits[2:]
        if digits.startswith("0"):
            return "+" + self._country_code + digits[1:]
        return "+" + digits
