"""Regression tests for exclusive phone ownership and one-call lifecycle.

Note: Characterizes surviving Cascade lifecycle behavior following S2S removal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from pipecat.frames.frames import TTSSpeakFrame

import phone_agent_gateway.ai_bridge.phone_voice_agent as voice_agent_module
from phone_agent_gateway.ai_bridge.call_context import CallContextPolicy
from phone_agent_gateway.ai_bridge.phone_voice_agent import PhoneVoiceAgent
from phone_agent_gateway.ai_bridge.production_pipeline import ProductionCallPipeline
from phone_agent_gateway.ai_bridge.runtime_config import RuntimeConfig
from phone_agent_gateway.ai_bridge.session import SessionPhase
from phone_agent_gateway.ai_bridge.voice_host_lock import VoiceHostBusyError, VoiceHostLock
from phone_agent_gateway.mac_client.framed_link import LinkError
from phone_agent_gateway.mac_client.gateway_client import CallState, CallStatus


def test_voice_host_lock_rejects_a_second_owner(tmp_path: Path) -> None:
    path = tmp_path / "voice.lock"
    first = VoiceHostLock(path)
    second = VoiceHostLock(path)
    first.acquire()
    try:
        with pytest.raises(VoiceHostBusyError, match="already running"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_direct_whatsapp_does_not_require_the_android_link_key() -> None:
    config = SimpleNamespace(link_authentication_key=None, call_channel="whatsapp")
    PhoneVoiceAgent(config)  # type: ignore[arg-type]


def test_gsm_still_requires_the_android_link_key() -> None:
    config = SimpleNamespace(link_authentication_key=None, call_channel="gsm")
    with pytest.raises(ValueError, match="PHONE_AGENT_LINK_KEY"):
        PhoneVoiceAgent(config)  # type: ignore[arg-type]


def test_voice_host_derives_direction_from_who_started_the_call() -> None:
    config = SimpleNamespace(link_authentication_key=b"x" * 32, call_channel="gsm")

    outbound = PhoneVoiceAgent(config, dial_number="+212600000000")  # type: ignore[arg-type]
    inbound = PhoneVoiceAgent(config)  # type: ignore[arg-type]

    assert outbound.call_direction == "outbound"
    assert inbound.call_direction == "inbound"


@pytest.mark.asyncio
async def test_voice_host_startup_sequence_finishes_before_gateway_is_declared_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        link_authentication_key=b"x" * 32,
        call_channel="gsm",
        voice_lock_path=tmp_path / "voice.lock",
    )
    agent = PhoneVoiceAgent(config)  # type: ignore[arg-type]
    order: list[str] = []

    async def step(name: str) -> None:
        order.append(name)

    async def gateway_ready(*, retry: bool = False) -> None:
        assert retry is True
        order.append("gateway_ready")
        agent._stopping.set()

    monkeypatch.setattr(agent, "_prewarm_primary_llm", lambda: step("llm"))
    monkeypatch.setattr(agent, "_prepare_provider_services", lambda: step("providers"))
    monkeypatch.setattr(agent, "_emit_voice_host_ready", lambda: order.append("verified"))
    monkeypatch.setattr(agent, "_replace_runtime", gateway_ready)
    monkeypatch.setattr(agent, "_close_runtime", lambda **_kwargs: step("closed"))

    await agent.run()

    assert order == ["llm", "providers", "verified", "gateway_ready", "closed"]


@pytest.mark.asyncio
async def test_outbound_agent_stops_after_call_returns_idle() -> None:
    config = SimpleNamespace(link_authentication_key=b"x" * 32, auto_answer=False)
    agent = PhoneVoiceAgent(config, dial_number="0600000000")  # type: ignore[arg-type]
    agent._runtime = SimpleNamespace(pipeline=object())  # type: ignore[assignment]

    await agent._handle_status(CallStatus("ok", CallState.ACTIVE, 4, "0600000000"))
    assert agent._stopping.is_set() is False

    await agent._handle_status(CallStatus("ok", CallState.IDLE, 0, ""))
    assert agent._stopping.is_set() is True


@pytest.mark.asyncio
async def test_voice_host_shutdown_hangs_up_active_channel_before_close() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_status(self) -> CallStatus:
            return CallStatus("ok", CallState.ACTIVE, 4, "+212600000000")

        def hangup(self) -> None:
            self.calls.append("hangup")

        def close(self) -> None:
            self.calls.append("close")

    class FakeSession:
        def __init__(self) -> None:
            self.phase = SessionPhase.ACTIVE
            self.metrics = {
                "input_frames": 0,
                "dropped_input_frames": 0,
                "sequence_gaps": 0,
                "stale_input_frames": 0,
                "output_frames": 0,
                "dropped_output_frames": 0,
            }

        def snapshot(self) -> SimpleNamespace:
            return SimpleNamespace(phase=self.phase, metrics=self.metrics)

        def set_phase(self, phase: SessionPhase) -> None:
            self.phase = phase

    config = SimpleNamespace(
        link_authentication_key=b"x" * 32,
        auto_answer=False,
        event_stream_enabled=False,
    )
    agent = PhoneVoiceAgent(config)  # type: ignore[arg-type]
    client = FakeClient()
    session = FakeSession()
    agent._runtime = SimpleNamespace(  # type: ignore[assignment]
        client=client,
        pipeline_start_task=None,
        pipeline=None,
        recorder=None,
        transport=None,
        session=session,
        phone_audio_route={},
    )

    await agent._close_runtime(hangup=True)

    assert client.calls == ["hangup", "close"]
    assert session.phase is SessionPhase.CLOSED
    assert agent._runtime is None





@pytest.mark.asyncio
async def test_failed_android_media_attach_is_attempted_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.attachments = 0
            self.hangups = 0

        def connect_media(self) -> None:
            self.attachments += 1
            raise RuntimeError("uplink peer closed")

        def get_audio_status(self) -> dict[str, object]:
            return {
                "audio": {
                    "last_error": "Telephony TX AudioTrack did not initialize",
                }
            }

        def hangup(self) -> None:
            self.hangups += 1

    async def no_sleep(_delay: float) -> None:
        return None

    config = SimpleNamespace(
        link_authentication_key=b"x" * 32,
        call_channel="gsm",
    )
    agent = PhoneVoiceAgent(config, dial_number="0600000000")  # type: ignore[arg-type]
    client = FakeClient()
    runtime = SimpleNamespace(client=client)
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(voice_agent_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(agent, "_emit_event", emitted.append)

    await agent._start_call(  # type: ignore[arg-type]
        runtime,
        CallStatus("ok", CallState.ACTIVE, 4, "0600000000"),
    )

    assert client.attachments == 1
    assert client.hangups == 1
    assert agent._stopping.is_set() is True
    assert "Telephony TX AudioTrack did not initialize" in str(emitted[-1]["message"])


@pytest.mark.asyncio
async def test_cascade_pipeline_receives_the_same_call_completion_callback() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.hangups = 0

        def hangup(self) -> None:
            self.hangups += 1

    config = SimpleNamespace(
        link_authentication_key=b"x" * 32,
        call_channel="gsm",
        pipeline_mode="cascade",
    )
    agent = PhoneVoiceAgent(config)  # type: ignore[arg-type]
    client = FakeClient()
    runtime = SimpleNamespace(
        client=client,
    )
    agent._runtime = runtime  # type: ignore[assignment]

    await agent._call_completion_sink(runtime)("AI ended call: caller finished")

    assert client.hangups == 1


@pytest.mark.asyncio
async def test_inbound_auto_answer_calls_gateway_answer() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def answer(self) -> None:
            self.calls.append("answer")

    config = SimpleNamespace(
        link_authentication_key=b"x" * 32,
        auto_answer=True,
        pipeline_mode="cascade",
        event_stream_enabled=False,
    )
    agent = PhoneVoiceAgent(config)  # type: ignore[arg-type]
    client = FakeClient()
    agent._runtime = SimpleNamespace(client=client, pipeline=None)  # type: ignore[assignment]

    await agent._handle_status(CallStatus("ok", CallState.RINGING, 2, "+212600000000"))

    assert agent.call_direction == "inbound"
    assert client.calls == ["answer"]

    await agent._handle_status(CallStatus("ok", CallState.RINGING, 2, "+212600000000"))
    assert client.calls == ["answer"]


@pytest.mark.asyncio
async def test_inbound_answer_state_race_does_not_break_phone_link() -> None:
    class FakeClient:
        def answer(self) -> None:
            raise LinkError("No ringing call")

    config = SimpleNamespace(
        link_authentication_key=b"x" * 32,
        auto_answer=True,
        pipeline_mode="cascade",
        event_stream_enabled=False,
    )
    agent = PhoneVoiceAgent(config)  # type: ignore[arg-type]
    agent._runtime = SimpleNamespace(client=FakeClient(), pipeline=None)  # type: ignore[assignment]

    await agent._handle_status(CallStatus("ok", CallState.RINGING, 2, "+212600000000"))

    assert agent._auto_answer_attempted is True


@pytest.mark.asyncio
async def test_greeting_is_deterministic_and_queued_once() -> None:
    queued: list[Any] = []
    finalized: list[tuple[str, str]] = []

    class FakePolicy:
        persona_compiler = SimpleNamespace(persona_data={"identity": {"name": "Adam"}})
        call_context = CallContextPolicy("outbound")

        async def finalize_response(self, text: str, *, response_kind: str = "turn"):
            finalized.append((text, response_kind))
            return text, object()

    class FakeWorker:
        async def queue_frame(self, frame: Any) -> None:
            queued.append(frame)

    pipeline = ProductionCallPipeline.__new__(ProductionCallPipeline)
    pipeline.policy = FakePolicy()
    pipeline.worker = FakeWorker()
    pipeline.config = SimpleNamespace(providers=SimpleNamespace(stt_language="en-US"))
    pipeline._greeted = False
    pipeline._greet_lock = asyncio.Lock()

    await asyncio.gather(pipeline.greet(), pipeline.greet(), pipeline.greet())

    assert len(queued) == 1
    assert isinstance(queued[0], TTSSpeakFrame)
    assert queued[0].append_to_context is True
    assert finalized == [
        ("Hello, this is Adam. Is now a good time for one question?", "greeting")
    ]


@pytest.mark.asyncio
async def test_sales_task_uses_oxzoon_outbound_opening_once() -> None:
    queued: list[Any] = []
    finalized: list[tuple[str, str]] = []

    class FakePolicy:
        persona_compiler = SimpleNamespace(persona_data={"identity": {"name": "Adam"}})
        call_context = CallContextPolicy("outbound")
        task_contract: ClassVar[dict[str, Any]] = {
            "opening_greeting": {
                "en": (
                    "Hello, this is Adam, Sales Manager at OXzoon. I'm calling about our "
                    "IPTV subscriptions. Is this a good time for a quick conversation?"
                )
            }
        }

        async def finalize_response(self, text: str, *, response_kind: str = "turn"):
            finalized.append((text, response_kind))
            return text, object()

    class FakeWorker:
        async def queue_frame(self, frame: Any) -> None:
            queued.append(frame)

    pipeline = ProductionCallPipeline.__new__(ProductionCallPipeline)
    pipeline.policy = FakePolicy()
    pipeline.worker = FakeWorker()
    pipeline.config = SimpleNamespace(providers=SimpleNamespace(stt_language="en-US"))
    pipeline._greeted = False
    pipeline._greet_lock = asyncio.Lock()

    await asyncio.gather(pipeline.greet(), pipeline.greet())

    assert len(queued) == 1
    assert finalized == [
        (
            "Hello, this is Adam, Sales Manager at OXzoon. I'm calling about our IPTV "
            "subscriptions. Is this a good time for a quick conversation?",
            "greeting",
        )
    ]


@pytest.mark.asyncio
async def test_voice_host_never_replays_opening_across_pipeline_recovery() -> None:
    config = SimpleNamespace(link_authentication_key=b"x" * 32, auto_answer=False)
    agent = PhoneVoiceAgent(config)  # type: ignore[arg-type]
    greeted: list[str] = []

    class FakePipeline:
        def __init__(self, name: str) -> None:
            self.name = name

        async def greet(self) -> None:
            greeted.append(self.name)

    await agent._greet_pipeline_once(FakePipeline("original"))  # type: ignore[arg-type]
    await agent._greet_pipeline_once(FakePipeline("recovered"))  # type: ignore[arg-type]

    assert greeted == ["original"]


@pytest.mark.asyncio
async def test_same_call_link_recovery_preserves_runtime_and_pipeline() -> None:
    config = SimpleNamespace(link_authentication_key=b"x" * 32, auto_answer=False)
    agent = PhoneVoiceAgent(config)  # type: ignore[arg-type]
    reconnects = 0

    class FakeClient:
        def reconnect(self) -> None:
            nonlocal reconnects
            reconnects += 1

    runtime = SimpleNamespace(
        client=FakeClient(),
        session=SimpleNamespace(call_id="call-1", link_epoch="epoch-2"),
        pipeline=object(),
    )
    agent._runtime = runtime  # type: ignore[assignment]

    await agent._recover_runtime_link(runtime)  # type: ignore[arg-type]

    assert agent._runtime is runtime
    assert runtime.pipeline is not None
    assert reconnects == 1


def _commanded_host(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> PhoneVoiceAgent:
    monkeypatch.setenv("PHONE_AGENT_COMMAND_STDIN", "true")
    monkeypatch.setenv("PHONE_AGENT_LINK_KEY_BASE64", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    config = RuntimeConfig.from_env(require_provider_credentials=False)
    return PhoneVoiceAgent(config, **kwargs)  # type: ignore[arg-type]


def test_voice_host_ready_reports_effective_parsed_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _commanded_host(monkeypatch)
    events: list[dict[str, object]] = []
    agent._emit_event = events.append  # type: ignore[method-assign]

    agent._emit_voice_host_ready()

    assert events[0]["type"] == "voice_host_ready"
    reported = events[0]["config"]
    assert isinstance(reported, dict)
    assert reported["stt_provider"] == agent.config.providers.stt_provider
    assert reported["stt_model"] == agent.config.providers.stt_model
    assert reported["llm_model"] == agent.config.providers.llm_model
    assert reported["tts_voice_id"] == agent.config.providers.tts_voice_id
    assert reported["task_id"] == agent.config.task_id
    assert reported["auto_answer"] == agent.config.auto_answer
    assert len(str(reported["system_prompt_sha256"])) == 64


def test_a_commanded_host_outlives_its_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exiting after one call is what forced a full model reload per dial.
    assert _commanded_host(monkeypatch)._one_call_mode is False
    assert _commanded_host(monkeypatch, dial_number="+212600000000")._one_call_mode is False


def test_the_one_shot_cli_form_still_stops_after_its_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHONE_AGENT_COMMAND_STDIN", raising=False)
    monkeypatch.setenv("PHONE_AGENT_LINK_KEY_BASE64", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    config = RuntimeConfig.from_env(require_provider_credentials=False)

    assert PhoneVoiceAgent(config, dial_number="+212600000000")._one_call_mode is True
    assert PhoneVoiceAgent(config)._one_call_mode is False


@pytest.mark.asyncio
async def test_a_dial_command_clears_the_previous_call_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resident host must not leak one caller's state into the next call."""

    agent = _commanded_host(monkeypatch)
    dialled: list[str] = []

    async def place(_runtime: object, number: str) -> dict[str, str]:
        dialled.append(number)
        return {"status": "ok"}

    agent._runtime = SimpleNamespace(client=SimpleNamespace())  # type: ignore[assignment]
    agent._place_outbound_call = place  # type: ignore[assignment]
    agent.call_direction = "inbound"
    agent._greeting_attempted = True
    agent._outbound_seen_live_state = True
    agent._active_caller_id = "+212000000000"

    await agent._handle_command({"command": "dial", "number": "+212600000000"})

    assert dialled == ["+212600000000"]
    assert agent.call_direction == "outbound"
    assert agent._outbound_number == "+212600000000"
    assert agent._greeting_attempted is False
    assert agent._outbound_seen_live_state is False
    assert agent._active_caller_id == ""


@pytest.mark.asyncio
async def test_unusable_commands_never_take_the_host_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _commanded_host(monkeypatch)

    await agent._handle_command({"command": "dial", "number": ""})
    await agent._handle_command({"command": "nonsense"})
    assert agent._stopping.is_set() is False

    await agent._handle_command({"command": "shutdown"})
    assert agent._stopping.is_set() is True
