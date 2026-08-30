"""The WhatsApp-via-phone dial path, and the guarantee that GSM is untouched."""

from __future__ import annotations

import inspect
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from phone_agent_gateway.ai_bridge.whatsapp_phone_client import (
    WhatsAppPhoneClient,
    WhatsAppPhoneError,
)

CALL_BUTTON = (
    '<node content-desc="Voice call" clickable="true" bounds="[1378,56][1474,152]" />'
)


class FakeCellularClient:
    """Stands in for the real client, recording what is delegated to it."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.routes: list[str] = []
        # dial() selects the VoIP route over this link before opening WhatsApp.
        self.link = SimpleNamespace(
            request=lambda command, payload: (
                self.routes.append(payload["route"]) or {"status": "ok"}
            )
        )

    def dial(self, number: str) -> dict[str, str]:
        self.calls.append(f"dial:{number}")
        return {"status": "cellular"}

    def flush_audio(self, advance_generation: bool = True) -> dict[str, str]:
        self.calls.append("flush")
        return {"status": "ok"}

    def hangup(self) -> dict[str, str]:
        self.calls.append("hangup")
        return {"status": "ok"}


def fake_adb(responses: dict[str, str], recorder: list[list[str]]):
    """Reply to adb by matching a fragment of the command."""

    def run(command, **kwargs):
        recorder.append(command)
        joined = " ".join(command)
        for fragment, stdout in responses.items():
            if fragment in joined:
                return subprocess.CompletedProcess(command, 0, stdout, "")
        if "packages -U" in joined:
            return subprocess.CompletedProcess(
                command, 0, "package:com.whatsapp uid:10141\n", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


IN_CALL = (
    "  mAudioModeOwner: AudioModeInfo: mMode=MODE_IN_COMMUNICATION, "
    "mPid=14574, mUid=10141\n"
)
NOT_IN_CALL = "  mAudioModeOwner: AudioModeInfo: mMode=MODE_NORMAL, mPid=0, mUid=0\n"


@pytest.fixture
def installed():
    return {
        "packages -U": "package:com.whatsapp uid:10141\n",
        "pm list packages": "package:com.whatsapp\n",
        "dumpsys audio": IN_CALL,
        "cat /sdcard": f"<hierarchy>{CALL_BUTTON}</hierarchy>",
    }


def test_dial_opens_the_chat_and_taps_the_call_button(installed):
    commands: list[list[str]] = []
    client = WhatsAppPhoneClient(FakeCellularClient(), device_id="abc123")
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb(installed, commands)
    ):
        result = client.dial("00212600454425")

    assert result["via"] == "whatsapp_phone"
    start = next(c for c in commands if "start" in c)
    assert "https://wa.me/212600454425" in start
    # The device is addressed explicitly so a second attached device is not dialled.
    assert start[:3] == ["adb", "-s", "abc123"]
    # Tapped at the centre of the reported bounds, not a hardcoded coordinate.
    tap = next(c for c in commands if "tap" in c)
    assert tap[-2:] == ["1426", "104"]


def test_dial_needs_no_contact_entry(installed):
    """A sales dialer calls numbers that are not in the address book."""

    commands: list[list[str]] = []
    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb(installed, commands)
    ):
        client.dial("+212600454425")

    assert not any("contacts" in " ".join(c) for c in commands)


def test_a_number_not_on_whatsapp_is_reported(installed):
    """No call control ever appears; the dial must fail rather than hang."""

    responses = dict(installed)
    responses["cat /sdcard"] = "<hierarchy></hierarchy>"
    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb(responses, [])
    ), patch(
        "phone_agent_gateway.ai_bridge.whatsapp_phone_client.UI_READY_TIMEOUT_SECS", 0.1
    ), patch(
        "phone_agent_gateway.ai_bridge.whatsapp_phone_client.UI_POLL_SECS", 0.01
    ), pytest.raises(WhatsAppPhoneError, match="may not be on WhatsApp"):
        client.dial("+212600454425")


def test_dial_never_reaches_the_cellular_dialer(installed):
    """The whole point: WhatsApp calls must not go through the GSM dialer."""

    inner = FakeCellularClient()
    client = WhatsAppPhoneClient(inner)
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb(installed, [])
    ):
        client.dial("+212600454425")

    assert inner.calls == []


def test_every_other_call_is_the_untouched_cellular_client():
    """Only dial is overridden; audio and lifecycle stay on the tuned path."""

    inner = FakeCellularClient()
    client = WhatsAppPhoneClient(inner)

    client.flush_audio()
    client.hangup()

    assert inner.calls == ["flush", "hangup"]
    assert client.link is inner.link  # the same link object, not a copy


def test_missing_whatsapp_is_reported_not_guessed_at():
    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb({}, [])
    ), pytest.raises(WhatsAppPhoneError, match="not installed"):
        client.dial("+212600454425")


def test_a_localized_call_button_is_still_found():
    """WhatsApp follows the device language; the button text is translated."""

    responses = {
        "packages -U": "package:com.whatsapp uid:10141\n",
        "pm list packages": "package:com.whatsapp\n",
        "dumpsys audio": IN_CALL,
        "cat /sdcard": (
            '<hierarchy><node content-desc="Appel vocal" clickable="true" '
            'bounds="[10,20][30,40]" /></hierarchy>'
        ),
    }
    commands: list[list[str]] = []
    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb(responses, commands)
    ):
        client.dial("+212600454425")

    assert next(c for c in commands if "tap" in c)[-2:] == ["20", "30"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("00212600454425", "+212600454425"),
        ("+212600454425", "+212600454425"),
        ("0600454425", "+212600454425"),
        ("06 00 45 44 25", "+212600454425"),
    ],
)
def test_numbers_reach_the_international_form_contacts_are_stored_as(given, expected):
    client = WhatsAppPhoneClient(FakeCellularClient(), country_code="212")
    assert client._normalize(given) == expected


def test_route_selection_uses_the_real_clients_transport():
    """Guards against wiring the route to a transport the client does not have.

    The first version called ``_request`` — an HTTP helper that exists on the
    unauthenticated client but not the authenticated one the agent actually
    builds. Every WhatsApp call would have died on connect_media. A fake cannot
    catch that, so the real class is inspected here.
    """

    from phone_agent_gateway.mac_client.protocol_client import (
        AuthenticatedPhoneAgentClient,
    )

    assert not hasattr(AuthenticatedPhoneAgentClient, "_request")
    # The framed link is how every other call command travels.
    assert hasattr(AuthenticatedPhoneAgentClient, "connect_media")
    source = inspect.getsource(WhatsAppPhoneClient._select_route)
    assert "link.request" in source
    assert "audio.route" in source


def test_the_phone_is_returned_to_cellular_on_close():
    """The injector policy diverts every app's mic; leaving it on breaks GSM."""

    routes: list[str] = []

    class Client(FakeCellularClient):
        def __init__(self) -> None:
            super().__init__()
            self.link = SimpleNamespace(
                request=lambda command, payload: (
                    routes.append(payload["route"]) or {"status": "ok"}
                )
            )

        def connect_media(self) -> None:
            self.calls.append("connect_media")

        def close(self) -> None:
            self.calls.append("close")

    client = WhatsAppPhoneClient(Client())
    client.connect_media()
    client.close()

    assert routes == ["voip", "cellular"]


def test_closing_still_completes_when_the_phone_cannot_be_restored():
    """A failed restore must not swallow the close it was attached to."""

    class Client(FakeCellularClient):
        def __init__(self) -> None:
            super().__init__()
            self.link = SimpleNamespace(
                request=lambda command, payload: {"status": "error", "message": "gone"}
            )

        def close(self) -> None:
            self.calls.append("close")

    inner = Client()
    WhatsAppPhoneClient(inner).close()

    assert inner.calls == ["close"]


CONFIRM_DIALOG = (
    '<node resource-id="android:id/button1" clickable="true" '
    'bounds="[1154,384][1282,480]" />'
)


def test_the_call_confirmation_prompt_is_answered():
    """WhatsApp confirms before calling a non-contact — every number for a dialer.

    Without this the call sits on the dialog forever: the phone shows a prompt
    nobody taps, and the pipeline waits for a call that never goes active.
    """

    state = {"tapped_call": False, "confirmed": False}
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(command)
        joined = " ".join(command)
        if "packages -U" in joined:
            return subprocess.CompletedProcess(
                command, 0, "package:com.whatsapp uid:10141\n", ""
            )
        if "pm list packages" in joined:
            return subprocess.CompletedProcess(command, 0, "package:com.whatsapp\n", "")
        if "dumpsys audio" in joined:
            # The call only comes up once the prompt has been confirmed.
            return subprocess.CompletedProcess(
                command, 0, IN_CALL if state["confirmed"] else NOT_IN_CALL, ""
            )
        if "uiautomator" in joined:
            screen = CONFIRM_DIALOG if state["tapped_call"] else CALL_BUTTON
            return subprocess.CompletedProcess(
                command, 0, f"<hierarchy>{screen}</hierarchy>", ""
            )
        if "input tap" in joined:
            if state["tapped_call"]:
                state["confirmed"] = True
            state["tapped_call"] = True
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=run
    ), patch(
        "phone_agent_gateway.ai_bridge.whatsapp_phone_client.WAKE_SETTLE_SECS", 0
    ):
        client.dial("+212600454425")

    taps = [c[-2:] for c in commands if "tap" in c]
    # First the call button, then the dialog's confirming button.
    assert taps == [["1426", "104"], ["1218", "432"]]


def test_a_saved_contact_dials_without_a_prompt(installed):
    """No prompt is normal, not a failure — it must not raise or hang."""

    commands: list[list[str]] = []
    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb(installed, commands)
    ), patch(
        "phone_agent_gateway.ai_bridge.whatsapp_phone_client.CONFIRM_TIMEOUT_SECS", 0.1
    ), patch(
        "phone_agent_gateway.ai_bridge.whatsapp_phone_client.UI_POLL_SECS", 0.01
    ):
        result = client.dial("+212600454425")

    assert result["status"] == "ok"


def test_a_dial_that_never_places_a_call_fails_loudly():
    """The worst outcome is a phantom call reported as success.

    A tap can land on a screen that has already changed, leaving WhatsApp on a
    chat or a picker. That once returned success, so the agent connected media
    and spoke for thirty seconds into a call nobody had placed.
    """

    responses = {
        "packages -U": "package:com.whatsapp uid:10141\n",
        "pm list packages": "package:com.whatsapp\n",
        "cat /sdcard": f"<hierarchy>{CALL_BUTTON}</hierarchy>",
        "dumpsys audio": NOT_IN_CALL,
    }
    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb(responses, [])
    ), patch(
        "phone_agent_gateway.ai_bridge.whatsapp_phone_client.CALL_START_TIMEOUT_SECS", 0.1
    ), patch(
        "phone_agent_gateway.ai_bridge.whatsapp_phone_client.CONFIRM_TIMEOUT_SECS", 0.05
    ), patch(
        "phone_agent_gateway.ai_bridge.whatsapp_phone_client.UI_POLL_SECS", 0.01
    ), pytest.raises(WhatsAppPhoneError, match="never started a call"):
        client.dial("+212600454425")


def test_another_apps_voip_call_is_not_mistaken_for_ours():
    """The audio mode is global; only WhatsApp's own ownership counts."""

    other_app = (
        "  mAudioModeOwner: AudioModeInfo: mMode=MODE_IN_COMMUNICATION, "
        "mPid=99, mUid=10999\n"
    )
    responses = {
        "packages -U": "package:com.whatsapp uid:10141\n",
        "dumpsys audio": other_app,
    }
    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb(responses, [])
    ):
        assert client._voip_call_in_progress() is False


def test_a_stale_ui_dump_is_never_used():
    """A dump that failed leaves the previous screen behind; it must be ignored."""

    def run(command, **kwargs):
        joined = " ".join(command)
        if "uiautomator" in joined:
            return subprocess.CompletedProcess(command, 1, "", "ERROR: could not dump")
        if "cat /sdcard" in joined:
            return subprocess.CompletedProcess(command, 0, "<hierarchy>stale</hierarchy>", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=run
    ):
        assert client._ui_dump() == ""


def test_the_phone_is_woken_before_the_ui_is_driven(installed):
    """A gateway phone locks between calls, and a locked screen breaks the dial.

    The accessibility tree then describes the keyguard rather than WhatsApp, so
    the call button is never found and taps land on whatever the lock screen has
    at those coordinates.
    """

    commands: list[list[str]] = []
    client = WhatsAppPhoneClient(FakeCellularClient())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=fake_adb(installed, commands)
    ), patch(
        "phone_agent_gateway.ai_bridge.whatsapp_phone_client.WAKE_SETTLE_SECS", 0
    ):
        client.dial("+212600454425")

    order = [" ".join(c) for c in commands]
    woke = next(i for i, c in enumerate(order) if "KEYCODE_WAKEUP" in c)
    dismissed = next(i for i, c in enumerate(order) if "dismiss-keyguard" in c)
    opened = next(i for i, c in enumerate(order) if "wa.me" in c)
    assert woke < dismissed < opened, "the screen must be usable before WhatsApp opens"


def test_the_voip_route_is_selected_before_whatsapp_opens_its_microphone(installed):
    """Ordering that decides whether the agent is audible at all.

    Android binds a recording to an audio policy mix when the recorder starts.
    Selecting the route after the call began left WhatsApp on the real
    microphone: nothing drained the injector track, its writes stalled, and the
    caller heard silence with no error anywhere.
    """

    order: list[str] = []

    class Client(FakeCellularClient):
        def __init__(self) -> None:
            super().__init__()
            self.link = SimpleNamespace(
                request=lambda command, payload: (
                    order.append(f"route:{payload['route']}") or {"status": "ok"}
                )
            )

    def run(command, **kwargs):
        joined = " ".join(command)
        if "wa.me" in joined:
            order.append("whatsapp opened")
        for fragment, stdout in installed.items():
            if fragment in joined:
                return subprocess.CompletedProcess(command, 0, stdout, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    client = WhatsAppPhoneClient(Client())
    with patch("shutil.which", return_value="/usr/bin/adb"), patch(
        "subprocess.run", side_effect=run
    ), patch("phone_agent_gateway.ai_bridge.whatsapp_phone_client.WAKE_SETTLE_SECS", 0):
        client.dial("+212600454425")

    assert order.index("route:voip") < order.index("whatsapp opened")
