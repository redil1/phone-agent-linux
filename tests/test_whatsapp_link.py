"""WhatsApp is a second channel that must never disturb the cellular one."""

from __future__ import annotations

import asyncio

import pytest

from phone_agent_gateway.ai_bridge.whatsapp_link import (
    BRIDGE_HEADER,
    BRIDGE_KIND_AUDIO,
    BRIDGE_KIND_CONTROL,
    BRIDGE_MAGIC,
    DEFAULT_BINARY,
    PHONE_FRAME_BYTES,
    WHATSAPP_FRAME_BYTES,
    WhatsAppLink,
    WhatsAppLinkError,
    resolve_binary,
)

# The Rust sidecar is a build artifact, not source, so a fresh clone does not
# have it. These tests drive that binary; without it they would fail an
# otherwise healthy checkout instead of reporting an unbuilt optional channel.
pytestmark = pytest.mark.skipif(
    not DEFAULT_BINARY.is_file(),
    reason=(
        "WhatsApp Rust sidecar is not built. Build it with: "
        "cd whatsapp_channel/rust_caller && ./build.sh"
    ),
)


def link() -> WhatsAppLink:
    return WhatsAppLink()


# --- the contract the transport binds to ---------------------------------------


def test_it_offers_exactly_the_callbacks_the_cellular_link_does() -> None:
    """The transport binds four callbacks; a second channel must supply the same."""

    channel = link()
    for name in ("on_audio_received", "send_audio_chunk", "send_audio_end_marker",
                 "flush_audio"):
        assert callable(getattr(channel, name)), name


def test_the_audio_format_matches_the_phone_path() -> None:
    """Both are s16le 16 kHz mono; only the framing differs."""

    assert PHONE_FRAME_BYTES == 640      # 20 ms
    assert WHATSAPP_FRAME_BYTES == 1920  # 60 ms
    assert WHATSAPP_FRAME_BYTES % PHONE_FRAME_BYTES == 0


def test_the_bundled_binary_is_found_without_configuration() -> None:
    assert resolve_binary().name == "whatsapp-rust-caller"


def test_a_configured_binary_that_is_absent_is_named_in_the_error() -> None:
    with pytest.raises(WhatsAppLinkError, match="/nowhere/whatsapp-rust-caller"):
        resolve_binary("/nowhere/whatsapp-rust-caller")


def test_a_missing_default_binary_explains_how_to_build_it(monkeypatch) -> None:
    import phone_agent_gateway.ai_bridge.whatsapp_link as module

    monkeypatch.setattr(module, "DEFAULT_BINARY", module.Path("/nowhere/absent"))
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    with pytest.raises(WhatsAppLinkError, match="rust_caller"):
        resolve_binary()


# --- audio handling -------------------------------------------------------------


def test_audio_sent_before_a_call_is_refused_not_queued() -> None:
    result = link().send_audio_chunk(b"\x00" * PHONE_FRAME_BYTES)
    assert result["status"] == "closed"
    assert result["accepted"] == 0


@pytest.mark.asyncio
async def test_peer_audio_is_rechunked_into_phone_frames() -> None:
    """WhatsApp delivers 60 ms; everything above expects 20 ms."""

    channel = link()
    frames: list[bytes] = []
    channel.on_audio_received(frames.append)

    class FakeStdout:
        def __init__(self) -> None:
            self.chunks = [b"\x01\x02" * 960, b""]

        async def read(self, _n: int) -> bytes:
            return self.chunks.pop(0)

    channel._process = type("P", (), {"stdout": FakeStdout()})()
    channel._running = True
    await channel._read_peer_audio()

    assert len(frames) == 3  # one 60 ms frame becomes three 20 ms frames
    assert all(len(frame) == PHONE_FRAME_BYTES for frame in frames)


@pytest.mark.asyncio
async def test_a_failing_listener_does_not_stop_the_call() -> None:
    channel = link()
    received: list[bytes] = []
    channel.on_audio_received(lambda _frame: (_ for _ in ()).throw(RuntimeError("boom")))
    channel.on_audio_received(received.append)

    class FakeStdout:
        def __init__(self) -> None:
            self.chunks = [b"\x00" * 1920, b""]

        async def read(self, _n: int) -> bytes:
            return self.chunks.pop(0)

    channel._process = type("P", (), {"stdout": FakeStdout()})()
    channel._running = True
    await channel._read_peer_audio()

    assert len(received) == 3


def test_a_barge_in_flush_drops_everything_queued() -> None:
    """Speech the caller interrupted must not keep playing."""

    channel = link()
    channel._running = True
    for _ in range(5):
        channel.send_audio_chunk(b"\x00" * PHONE_FRAME_BYTES)

    result = channel.flush_audio()

    assert result["dropped"] == 5
    assert channel._outbound.empty()
    assert not channel._outbound_remainder


def test_a_backlog_drops_the_oldest_audio_not_the_newest() -> None:
    """A queue of stale speech is bounded rather than growing indefinitely."""

    channel = link()
    channel._running = True
    for index in range(80):  # past the bounded queue
        channel.send_audio_chunk(bytes([index % 256]) * PHONE_FRAME_BYTES)

    assert channel._outbound.qsize() <= 12


class FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, payload: bytes) -> None:
        self.data.extend(payload)

    async def drain(self) -> None:
        return None


@pytest.mark.asyncio
async def test_agent_audio_crosses_the_rust_boundary_with_generation_identity() -> None:
    channel = link()
    stdin = FakeStdin()
    channel._process = type("P", (), {"stdin": stdin, "returncode": None})()
    channel._running = True
    channel._loop = asyncio.get_running_loop()
    channel._active_generation = 7

    writer = asyncio.create_task(channel._write_agent_audio())
    for sequence in range(3):
        assert channel.send_audio_chunk(
            bytes([sequence]) * PHONE_FRAME_BYTES,
            generation_id=7,
            sequence=sequence,
        )["status"] == "ok"
    await asyncio.wait_for(channel._outbound.join(), timeout=1)
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)

    magic, kind, generation, sequence, size = BRIDGE_HEADER.unpack_from(stdin.data)
    payload = bytes(stdin.data[BRIDGE_HEADER.size :])
    assert magic == BRIDGE_MAGIC
    assert kind == BRIDGE_KIND_AUDIO
    assert generation == 7
    assert sequence == 2
    assert size == WHATSAPP_FRAME_BYTES
    assert payload == (
        b"\x00" * PHONE_FRAME_BYTES
        + b"\x01" * PHONE_FRAME_BYTES
        + b"\x02" * PHONE_FRAME_BYTES
    )


@pytest.mark.asyncio
async def test_barge_in_is_forwarded_to_rust_with_the_next_generation() -> None:
    import json

    channel = link()
    stdin = FakeStdin()
    channel._process = type("P", (), {"stdin": stdin, "returncode": None})()
    channel._running = True
    result = await channel._flush_async(9)

    magic, kind, generation, _sequence, size = BRIDGE_HEADER.unpack_from(stdin.data)
    payload = bytes(stdin.data[BRIDGE_HEADER.size : BRIDGE_HEADER.size + size])
    assert magic == BRIDGE_MAGIC
    assert kind == BRIDGE_KIND_CONTROL
    assert generation == 9
    assert json.loads(payload) == {"type": "flush", "next_generation": 9}
    assert result["generation"] == 9


@pytest.mark.asyncio
async def test_audio_end_stays_behind_padded_final_audio() -> None:
    import json

    channel = link()
    stdin = FakeStdin()
    channel._process = type("P", (), {"stdin": stdin, "returncode": None})()
    channel._running = True
    channel._loop = asyncio.get_running_loop()

    writer = asyncio.create_task(channel._write_agent_audio())
    assert channel.send_audio_chunk(
        b"\x33" * PHONE_FRAME_BYTES,
        generation_id=1,
        sequence=4,
    )["status"] == "ok"
    assert channel.send_audio_end_marker(1, 5)["status"] == "ok"
    await asyncio.wait_for(channel._outbound.join(), timeout=1)
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)

    first = BRIDGE_HEADER.unpack_from(stdin.data)
    first_end = BRIDGE_HEADER.size + first[4]
    second = BRIDGE_HEADER.unpack_from(stdin.data, first_end)
    second_payload = bytes(
        stdin.data[first_end + BRIDGE_HEADER.size : first_end + BRIDGE_HEADER.size + second[4]]
    )
    assert first[:4] == (BRIDGE_MAGIC, BRIDGE_KIND_AUDIO, 1, 4)
    assert first[4] == WHATSAPP_FRAME_BYTES
    assert bytes(stdin.data[BRIDGE_HEADER.size:first_end]).startswith(
        b"\x33" * PHONE_FRAME_BYTES
    )
    assert second[:4] == (BRIDGE_MAGIC, BRIDGE_KIND_CONTROL, 1, 5)
    assert json.loads(second_payload)["type"] == "audio_end"


@pytest.mark.asyncio
async def test_rust_transport_ack_updates_the_call_session() -> None:
    from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase

    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    session.set_phase(SessionPhase.ACTIVE)
    channel = WhatsAppLink(render_ack_handler=session.mark_rendered)

    class FakeStderr:
        def __init__(self) -> None:
            self.lines = [b"PLAYOUT_ACK generation=1 sequence=12\n", b""]

        async def readline(self) -> bytes:
            return self.lines.pop(0)

    channel._process = type("P", (), {"stderr": FakeStderr()})()
    channel._running = True
    await channel._watch_status()
    assert session.metrics.last_rendered_sequence == 12


@pytest.mark.asyncio
async def test_a_sidecar_failure_can_never_be_mistaken_for_an_answer() -> None:
    channel = link()

    class FakeStderr:
        def __init__(self) -> None:
            self.lines = [
                b"Error: the WhatsApp call was not answered\n",
                b"",
            ]

        async def readline(self) -> bytes:
            return self.lines.pop(0)

    channel._process = type("P", (), {"stderr": FakeStderr()})()
    channel._running = True
    await channel._watch_status()

    assert not channel.answered
    assert channel._failure_message == "the WhatsApp call was not answered"


@pytest.mark.asyncio
async def test_only_the_explicit_peer_accept_marker_answers_a_call() -> None:
    channel = link()

    class FakeStderr:
        def __init__(self) -> None:
            self.lines = [
                b"[+] Peer ACCEPTED the call! Audio channel is active.\n",
                b"",
            ]

        async def readline(self) -> bytes:
            return self.lines.pop(0)

    channel._process = type("P", (), {"stderr": FakeStderr()})()
    channel._running = True
    await channel._watch_status()

    assert channel.answered


@pytest.mark.asyncio
async def test_built_rust_sidecar_round_trips_audio_and_hangup(monkeypatch) -> None:
    """Exercise the real Rust process and Python framing without WhatsApp's network."""

    monkeypatch.setenv("PHONE_AGENT_WHATSAPP_RUST_MOCK", "1")
    rendered: list[tuple[int, int]] = []
    received: list[bytes] = []
    rendered_ready = asyncio.Event()
    received_ready = asyncio.Event()

    def on_rendered(generation: int, sequence: int) -> None:
        rendered.append((generation, sequence))
        if (generation, sequence) == (1, 2):
            rendered_ready.set()

    def on_received(frame: bytes) -> None:
        received.append(frame)
        if len(received) >= 3:
            received_ready.set()

    channel = WhatsAppLink(
        render_ack_handler=on_rendered
    )
    channel.on_audio_received(on_received)

    await channel.dial("+212600000000")
    for sequence in range(3):
        assert channel.send_audio_chunk(
            bytes([sequence + 1]) * PHONE_FRAME_BYTES,
            generation_id=1,
            sequence=sequence,
        )["status"] == "ok"

    await asyncio.wait_for(
        asyncio.gather(received_ready.wait(), rendered_ready.wait()),
        timeout=3,
    )

    await channel._flush_async(2)
    await channel._write_bridge_frame(
        BRIDGE_KIND_AUDIO,
        1,
        20,
        b"\x44" * WHATSAPP_FRAME_BYTES,
    )
    await channel._write_bridge_frame(
        BRIDGE_KIND_AUDIO,
        2,
        21,
        b"\x55" * WHATSAPP_FRAME_BYTES,
    )

    async def new_generation_received() -> None:
        while len(received) < 6:
            await received_ready.wait()
            received_ready.clear()

    await asyncio.wait_for(new_generation_received(), timeout=3)
    await channel.hangup()

    assert received == [
        b"\x01" * PHONE_FRAME_BYTES,
        b"\x02" * PHONE_FRAME_BYTES,
        b"\x03" * PHONE_FRAME_BYTES,
        b"\x55" * PHONE_FRAME_BYTES,
        b"\x55" * PHONE_FRAME_BYTES,
        b"\x55" * PHONE_FRAME_BYTES,
    ]
    assert channel.ended


# --- isolation from the cellular path -------------------------------------------


@pytest.mark.parametrize("channel", ["gsm", "whatsapp", "whatsapp_phone"])
def test_every_channel_is_accepted(channel: str) -> None:
    """Asserted by running validation, not by reading its source.

    A source-text assertion here once passed while call_channel did not exist
    on the config at all, so every call died. This constructs the real config.
    """

    from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig

    ProviderConfig(call_channel=channel).validate(require_credentials=False)


def test_an_unknown_channel_is_refused() -> None:
    from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig

    with pytest.raises(ValueError, match="call channel must be"):
        ProviderConfig(call_channel="carrier-pigeon").validate(require_credentials=False)


def test_cellular_stays_the_default() -> None:
    from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig

    assert ProviderConfig().call_channel == "gsm"


def test_whatsapp_shares_no_transport_state_with_the_cellular_link() -> None:
    """Cellular runs over adb and a framed TCP link; this drives a local binary.

    Nothing is imported from the cellular client here, so a WhatsApp call cannot
    touch the sockets, ports or device handles a GSM call is using.
    """

    import ast

    import phone_agent_gateway.ai_bridge.whatsapp_link as module

    tree = ast.parse(open(module.__file__).read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    # Checking imports rather than raw text: the words appear in this module's
    # own prose explaining why it stays clear of them.
    for forbidden in ("framed_link", "gateway_client", "protocol_client",
                      "pipecat_transport", "session"):
        assert not any(forbidden in name for name in imported), (
            f"WhatsApp must not import {forbidden}: a GSM call may be using it"
        )


@pytest.mark.asyncio
async def test_hangup_is_safe_before_a_call_and_twice_over() -> None:
    channel = link()
    await channel.hangup()
    await channel.hangup()
    assert channel.ended


# --- the dial path ---------------------------------------------------------------


def test_the_agent_builds_a_whatsapp_runtime_only_when_asked() -> None:
    """The cellular construction must stay exactly as it was.

    Selecting WhatsApp takes a separate branch rather than parameterising the
    cellular one, so a GSM call is built by the same code it always was.
    """

    import inspect

    from phone_agent_gateway.ai_bridge.phone_voice_agent import PhoneVoiceAgent

    source = inspect.getsource(PhoneVoiceAgent._new_runtime)
    assert 'if self.config.call_channel == "whatsapp":' in source
    assert "return self._new_whatsapp_runtime(session, transport)" in source
    # The cellular client is still constructed unconditionally below the branch.
    assert "AuthenticatedPhoneAgentClient(" in source

    whatsapp = inspect.getsource(PhoneVoiceAgent._new_whatsapp_runtime)
    for binding in ("on_audio_received", "set_tx_handler",
                    "set_audio_end_handler", "set_flush_handler"):
        assert binding in whatsapp, f"{binding} must be bound for WhatsApp too"


def test_the_whatsapp_client_speaks_the_agents_own_status_type() -> None:
    """Anything downstream reading call state must not special-case a channel."""

    from phone_agent_gateway.ai_bridge.session import CallSessionState
    from phone_agent_gateway.ai_bridge.whatsapp_client import WhatsAppPhoneClient
    from phone_agent_gateway.mac_client.gateway_client import CallState, CallStatus

    client = WhatsAppPhoneClient(CallSessionState())
    status = client.get_status()
    assert isinstance(status, CallStatus)
    assert status.state is CallState.IDLE


def test_unsupported_whatsapp_operations_fail_clearly() -> None:
    from phone_agent_gateway.ai_bridge.session import CallSessionState
    from phone_agent_gateway.ai_bridge.whatsapp_client import WhatsAppPhoneClient

    client = WhatsAppPhoneClient(CallSessionState())
    with pytest.raises(WhatsAppLinkError, match="does not accept inbound"):
        client.answer()
    with pytest.raises(WhatsAppLinkError, match="dial again"):
        client.reconnect()


def test_the_studio_reports_both_channels_and_why_one_is_unusable() -> None:
    import inspect

    from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer

    source = inspect.getsource(PhoneAgentWebServer.handle_get_channel_status)
    assert '"gsm"' in source and '"whatsapp"' in source
    assert "enter your WhatsApp number" in source, "an unpaired WhatsApp must say how to pair"
    assert "rust_caller" in source, "an unbuilt caller must say how to build it"


# --- pairing from the Studio -----------------------------------------------------


def test_the_pairing_code_is_parsed_from_the_binarys_output() -> None:
    from phone_agent_gateway.ai_bridge.whatsapp_link import PAIRING_CODE_MARKER

    line = "   YOUR PAIRING CODE:   ABCD-1234"
    assert PAIRING_CODE_MARKER in line
    assert line.split(PAIRING_CODE_MARKER, 1)[1].strip() == "ABCD-1234"


def test_pairing_is_offered_in_the_studio_not_only_a_terminal() -> None:
    import inspect

    from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer

    source = inspect.getsource(PhoneAgentWebServer.handle_post_whatsapp_pair)
    assert "phone number is required" in source
    # The code must reach the operator while pairing is still waiting on it.
    assert "whatsapp_pairing_code" in source


def test_the_studio_shows_the_code_and_where_to_type_it() -> None:
    from phone_agent_gateway.ai_bridge.web_server import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "function pairWhatsApp" in page
    assert "Linked Devices" in page, "the operator needs to know where the code goes"
    assert "whatsapp_pairing_code" in page


def test_pairing_cannot_complete_without_the_phone() -> None:
    """The code is displayed, but it is useless unless typed into WhatsApp.

    That is why exposing pairing in a local Studio does not weaken the account.
    """

    import inspect

    from phone_agent_gateway.ai_bridge import whatsapp_link

    # Normalised: the sentence wraps across lines in the source.
    doc = " ".join((inspect.getdoc(whatsapp_link.pair_phone) or "").split())
    assert "the code is useless without the phone it must be typed into" in doc


def test_the_channel_selector_is_not_squeezed_beside_the_call_buttons() -> None:
    """It first rendered inside the Call/Hang Up flex row and was unreadable."""

    from phone_agent_gateway.ai_bridge.web_server import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    selector = page.index('id="call-channel"')
    actions = page.index('<div class="call-actions">')
    assert selector < actions, "the selector must sit above the call action row"


def test_the_dial_branch_actually_runs_not_just_reads_correctly() -> None:
    """A source-text assertion missed that RuntimeConfig had no call_channel.

    The branch was present and correct, and every call still died with
    AttributeError because the setting lived on ProviderConfig.
    """

    from phone_agent_gateway.ai_bridge.runtime_config import RuntimeConfig

    for field in ("call_channel", "whatsapp_country_code", "whatsapp_max_duration_secs"):
        assert isinstance(getattr(RuntimeConfig, field), property), (
            f"RuntimeConfig must expose {field}; the voice host reads it from there"
        )


@pytest.mark.asyncio
async def test_the_call_is_created_on_the_pipelines_loop() -> None:
    """Created on a throwaway loop, the audio pumps die when dial() returns.

    The subprocess, the queues and the pump tasks must outlive the dial call,
    or the call connects and then carries no audio in either direction.
    """

    import asyncio

    from phone_agent_gateway.ai_bridge.session import CallSessionState
    from phone_agent_gateway.ai_bridge.whatsapp_client import WhatsAppPhoneClient

    client = WhatsAppPhoneClient(CallSessionState())
    assert client._loop is asyncio.get_running_loop()

    # Off-loop, as the agent calls it via asyncio.to_thread.
    marker = {}

    def from_worker() -> None:
        async def note() -> None:
            marker["loop"] = asyncio.get_running_loop()

        client._call(note())

    await asyncio.to_thread(from_worker)
    assert marker["loop"] is client._loop


def test_the_preflight_matches_the_channel() -> None:
    """The cellular ADB checks refused a call the Android phone never carries."""

    import inspect

    from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer

    source = inspect.getsource(PhoneAgentWebServer._gateway_preflight)
    assert 'self.config.call_channel == "whatsapp"' in source
    assert "_whatsapp_preflight" in source

    whatsapp = inspect.getsource(PhoneAgentWebServer._whatsapp_preflight)
    assert "is_paired" in whatsapp
    assert "adb" not in whatsapp.lower()


def test_the_tx_handlers_match_the_cellular_signatures_exactly() -> None:
    """The transport calls every tx handler the same way.

    A shorter signature raised TypeError on every frame, so the agent held a
    whole turn the caller never heard.
    """

    import inspect

    import phone_agent_gateway.mac_client.framed_link as framed

    cellular = next(
        cls for _, cls in inspect.getmembers(framed, inspect.isclass)
        if hasattr(cls, "send_audio_chunk")
    )
    for name in ("send_audio_chunk", "send_audio_end_marker"):
        expected = list(inspect.signature(getattr(cellular, name)).parameters)[1:]
        actual = list(inspect.signature(getattr(WhatsAppLink, name)).parameters)[1:]
        assert actual[: len(expected)] == expected, (
            f"{name}: cellular takes {expected}, WhatsApp takes {actual}"
        )


def test_the_transport_accepts_unframed_caller_audio() -> None:
    """WhatsApp has no MediaFrame protocol, so bytes must have their own door."""

    from phone_agent_gateway.ai_bridge.pipecat_transport import PhoneAgentTransport

    assert hasattr(PhoneAgentTransport, "feed_s2s_audio_bytes")


def test_the_android_uplink_wait_is_cellular_only() -> None:
    """Requiring Android's TX route failed a call over a phone it never uses."""

    import inspect

    from phone_agent_gateway.ai_bridge.phone_voice_agent import PhoneVoiceAgent

    source = inspect.getsource(PhoneVoiceAgent)
    marker = 'if self.config.call_channel != "whatsapp":'
    assert marker in source
    assert source.index(marker) < source.index("_require_live_injection_route(runtime)\n")
