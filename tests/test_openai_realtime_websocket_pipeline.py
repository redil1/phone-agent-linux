"""Regression tests for native S2S WebSocket audio and turn ownership."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import phone_agent_gateway.ai_bridge.openai_realtime_websocket_pipeline as websocket_module
import pytest
from phone_agent_gateway.ai_bridge.openai_realtime_websocket_pipeline import (
    CONVERSATION_REPLAY_TURNS,
    OUTPUT_QUEUE_WARN_FRAMES,
    PHONE_FRAME_BYTES,
    REALTIME_SAMPLE_RATE,
    STARTUP_STABILIZER_MAX_FRAMES,
    STARTUP_STABILIZER_MIN_FRAMES,
    OpenAIRealtimeWebSocketPipeline,
    _OutputQueueItem,
    _PhoneInputBridge,
    _StartupSpeechVerifier,
)
from phone_agent_gateway.ai_bridge.openwa_integration import (
    OpenWAConfig,
    OpenWAConfigStore,
)
from phone_agent_gateway.ai_bridge.pipecat_transport import (
    AudioWriteResult,
    PhoneAgentTransport,
)
from phone_agent_gateway.ai_bridge.runtime_config import (
    ConfigurationError,
    ProviderConfig,
    RuntimeConfig,
)
from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase
from phone_agent_gateway.ai_bridge.tool_control import (
    ManagedToolPolicy,
    ToolConnection,
    ToolControlConfig,
    ToolControlStore,
)
from phone_agent_gateway.ai_bridge.web_research import (
    WebResearchConfig,
    WebResearchConfigStore,
)
from pipecat.frames.frames import OutputAudioRawFrame


def runtime_config() -> RuntimeConfig:
    providers = ProviderConfig(
        pipeline_mode="s2s_chatgpt_realtime",
        chatgpt_realtime_transport="websocket",
        chatgpt_realtime_voice="alloy",
        stt_language="en-US",
    )
    return RuntimeConfig(
        device_id="test",
        control_host="127.0.0.1",
        control_port=8765,
        protocol_control_port=8768,
        rx_port=8766,
        tx_port=8767,
        sample_rate=16_000,
        frame_ms=20,
        input_queue_frames=25,
        auto_answer=False,
        record_calls=False,
        memory_enabled=True,
        task_id="iptv_subscription_sales",
        event_stream_enabled=True,
        voice_lock_path=Path("/tmp/test-phone-agent.lock"),
        system_prompt="",
        link_authentication_key=b"0" * 32,
        providers=providers,
    )


def active_transport() -> PhoneAgentTransport:
    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    session.set_phase(SessionPhase.ACTIVE)
    return PhoneAgentTransport(session=session)


def record_events(sent: list[dict]) -> Callable[[dict], bool]:
    """Stand in for send_event, honouring its accepted/dropped contract."""

    def send(event: dict) -> bool:
        sent.append(event)
        return True

    return send


def pipeline(*, events: list[dict] | None = None) -> OpenAIRealtimeWebSocketPipeline:
    return OpenAIRealtimeWebSocketPipeline(
        active_transport(),
        runtime_config(),
        auth_manager=MagicMock(),
        caller_id="+33123456789",
        event_sink=events.append if events is not None else None,
    )


@pytest.mark.asyncio
async def test_start_sends_session_update_and_waits_for_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            message = await self.incoming.get()
            if message is None:
                raise StopAsyncIteration
            return message

        async def send(self, message: str) -> None:
            event = websocket_module.json.loads(message)
            self.sent.append(event)
            if event["type"] == "session.update" and len(self.sent) == 1:
                await self.incoming.put('{"type":"session.updated","session":{"id":"s1"}}')

        async def close(self) -> None:
            await self.incoming.put(None)

    fake_ws = FakeWebSocket()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    monkeypatch.setattr(websocket_module, "connect", fake_connect)
    realtime = pipeline()
    realtime.auth_manager.get_token.return_value = "test-token"

    await realtime.start(timeout_secs=1.0)

    assert fake_ws.sent[0]["type"] == "session.update"
    assert fake_ws.sent[0]["session"]["model"] == "gpt-realtime-2.1"
    assert realtime._session_updated.is_set()
    await realtime.close()


@pytest.mark.asyncio
async def test_active_call_reconnects_and_resumes_without_repeating_greeting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
            self.response_sent = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            message = await self.incoming.get()
            if message is None:
                raise StopAsyncIteration
            return message

        async def send(self, message: str) -> None:
            event = websocket_module.json.loads(message)
            self.sent.append(event)
            if event["type"] == "response.create":
                self.response_sent.set()
            if event["type"] == "session.update" and len(self.sent) == 1:
                await self.incoming.put('{"type":"session.updated","session":{"id":"s1"}}')

        async def close(self) -> None:
            await self.incoming.put(None)

    sockets = [FakeWebSocket(), FakeWebSocket()]
    connect_count = 0

    async def fake_connect(*_args, **_kwargs):
        nonlocal connect_count
        socket = sockets[connect_count]
        connect_count += 1
        return socket

    monkeypatch.setattr(websocket_module, "connect", fake_connect)
    events: list[dict] = []
    realtime = pipeline(events=events)
    realtime.auth_manager.get_token.return_value = "test-token"

    await realtime.start(timeout_secs=1.0)
    await realtime.greet()
    await asyncio.wait_for(sockets[0].response_sent.wait(), timeout=2.0)
    await sockets[0].incoming.put(None)
    await asyncio.wait_for(sockets[1].response_sent.wait(), timeout=2.0)

    assert sum(
        event.get("event_id", "").startswith("phoneagent_greeting_")
        for socket in sockets
        for event in socket.sent
    ) == 1
    assert any(event["type"] == "realtime_reconnected" for event in events)
    await realtime.close()


@pytest.mark.asyncio
async def test_session_uses_native_pcm_server_vad_and_realtime_21() -> None:
    realtime = pipeline()

    session = realtime._build_session_update()["session"]

    assert session["model"] == "gpt-realtime-2.1"
    assert session["output_modalities"] == ["audio"]
    assert session["reasoning"] == {"effort": "low"}
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24_000}
    assert session["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24_000}
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 700,
        "create_response": True,
        "interrupt_response": True,
        "idle_timeout_ms": 8_000,
    }
    # Playback rate, not composed pacing: measured ~189 wpm on marin at 1.05
    # against 157-174 at 1.0, where the lower figure reads as unhurried.
    assert session["audio"]["output"]["speed"] == 1.05
    # keywords is accepted but never applied by the configured transcription
    # models, so the domain vocabulary has to ride in the prompt.
    transcription = session["audio"]["input"]["transcription"]
    assert "keywords" not in transcription
    assert "OXzoon" in transcription["prompt"]
    assert session["max_output_tokens"] == "inf"
    assert "delay" not in session["audio"]["input"]["transcription"]
    assert "clean, isolated call" in session["instructions"]
    assert "Current objective:" in session["instructions"]
    assert "Essential is 10 euros" in session["instructions"]
    assert any(tool["name"] == "end_call" for tool in session["tools"])
    assert "You—not a phrase matcher—decide" in session["instructions"]
    await realtime.close()


@pytest.mark.asyncio
async def test_greeting_disables_automatic_vad_before_audio_input_opens() -> None:
    realtime = pipeline()
    realtime.input_bridge = _PhoneInputBridge(asyncio.get_running_loop())
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime.greet()

    guarded_update = next(
        event
        for event in sent
        if event["type"] == "session.update"
        and event["event_id"].startswith("phoneagent_opening_vad_guard_")
    )
    turn_detection = guarded_update["session"]["audio"]["input"]["turn_detection"]
    assert turn_detection["create_response"] is False
    assert turn_detection["interrupt_response"] is False
    greeting_index = next(
        index
        for index, event in enumerate(sent)
        if event["type"] == "response.create"
        and event["response"]["metadata"]["phoneagent_kind"] == "greeting"
    )
    assert sent.index(guarded_update) < greeting_index
    assert realtime._opening_vad_guard_active is True
    await realtime.close()


@pytest.mark.asyncio
async def test_unconfirmed_opening_noise_cannot_cancel_or_answer() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime._opening_vad_guard_active = True
    realtime.input_bridge = MagicMock()
    realtime.input_bridge.has_recent_human_speech.return_value = False
    realtime.input_bridge.quality_snapshot.return_value = {}
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    await realtime._handle_event(
        {
            "type": "response.created",
            "response": {
                "id": "greeting-1",
                "metadata": {"phoneagent_kind": "greeting"},
            },
        }
    )
    greeting = realtime._responses["greeting-1"]
    greeting.audio_done = True

    await realtime._handle_event(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "startup-noise",
            "audio_start_ms": 0,
        }
    )
    await realtime._handle_event(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "startup-noise",
            "audio_end_ms": 928,
        }
    )
    await realtime._handle_event(
        {"type": "input_audio_buffer.committed", "item_id": "startup-noise"}
    )

    assert greeting.interrupted is False
    assert any(
        event["type"] == "conversation.item.delete"
        and event["item_id"] == "startup-noise"
        for event in sent
    )
    assert not any(event["type"] == "response.cancel" for event in sent)
    assert not any(
        event["type"] == "response.create"
        and event["response"].get("metadata", {}).get("phoneagent_kind") == "turn"
        for event in sent
    )

    await realtime._handle_event(
        {
            "type": "response.done",
            "response": {"id": "greeting-1", "status": "completed", "output": []},
        }
    )
    restored = [
        event
        for event in sent
        if event["type"] == "session.update"
        and event["event_id"].startswith("phoneagent_opening_vad_restore_")
    ]
    assert restored[-1]["session"]["audio"]["input"]["turn_detection"][
        "interrupt_response"
    ] is True
    await realtime.close()


@pytest.mark.asyncio
async def test_confirmed_human_opening_speech_interrupts_and_receives_one_answer() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime._opening_vad_guard_active = True
    realtime.transport.set_flush_handler(lambda _advance: {"status": "ok"})
    realtime.input_bridge = MagicMock()
    realtime.input_bridge.has_recent_human_speech.return_value = True
    realtime.input_bridge.quality_snapshot.return_value = {}
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    await realtime._handle_event(
        {
            "type": "response.created",
            "response": {
                "id": "greeting-1",
                "metadata": {"phoneagent_kind": "greeting"},
            },
        }
    )
    greeting = realtime._responses["greeting-1"]
    greeting.audio_done = True

    await realtime._handle_event(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "real-caller",
            "audio_start_ms": 0,
        }
    )
    await asyncio.sleep(0.1)
    assert greeting.interrupted is True
    assert any(event["type"] == "response.cancel" for event in sent)

    await realtime._handle_event(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "real-caller",
            "audio_end_ms": 800,
        }
    )
    await realtime._handle_event(
        {"type": "input_audio_buffer.committed", "item_id": "real-caller"}
    )
    await realtime._handle_event(
        {
            "type": "response.done",
            "response": {"id": "greeting-1", "status": "cancelled", "output": []},
        }
    )

    turn_responses = [
        event
        for event in sent
        if event["type"] == "response.create"
        and event["response"].get("metadata", {}).get("phoneagent_kind") == "turn"
    ]
    assert len(turn_responses) == 1
    assert not any(
        event["type"] == "conversation.item.delete"
        and event.get("item_id") == "real-caller"
        for event in sent
    )
    await realtime.close()


@pytest.mark.asyncio
async def test_idle_timeout_can_be_disabled() -> None:
    config = runtime_config()
    config = replace(
        config, providers=replace(config.providers, chatgpt_realtime_idle_timeout_ms=0)
    )
    realtime = OpenAIRealtimeWebSocketPipeline(
        active_transport(), config, auth_manager=MagicMock(), caller_id="+33123456789"
    )
    turn_detection = realtime._build_session_update()["session"]["audio"]["input"][
        "turn_detection"
    ]
    assert "idle_timeout_ms" not in turn_detection


def test_idle_timeout_below_the_server_floor_is_rejected() -> None:
    config = runtime_config()
    with pytest.raises(ConfigurationError):
        replace(
            config.providers, chatgpt_realtime_idle_timeout_ms=2_000
        ).validate(require_credentials=False)


@pytest.mark.asyncio
async def test_audio_deltas_never_block_the_websocket_reader() -> None:
    """The reader must stay free to deliver barge-in and error events."""

    realtime = pipeline()
    realtime._running = True
    realtime.send_event = record_events([])  # type: ignore[method-assign]
    await realtime._handle_event({"type": "response.created", "response": {"id": "r1"}})
    realtime._responses["r1"].first_audio_at = realtime._responses["r1"].created_at
    # Far more speech than any bounded queue would have admitted.
    frames = OUTPUT_QUEUE_WARN_FRAMES * 2
    delta = base64.b64encode(b"\x00" * (REALTIME_SAMPLE_RATE // 50 * 2)).decode()
    for _ in range(frames):
        await asyncio.wait_for(
            realtime._handle_event(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "item_id": "assistant-1",
                    "delta": delta,
                }
            ),
            timeout=1.0,
        )
    # Well past the old bounded cap, which would have blocked the reader here.
    assert realtime._output_queue.qsize() > OUTPUT_QUEUE_WARN_FRAMES
    # A barge-in arriving behind that audio is still handled immediately.
    realtime.transport.set_flush_handler(lambda _advance: {"status": "ok"})
    await asyncio.wait_for(
        realtime._handle_event(
            {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
        ),
        timeout=1.0,
    )
    assert realtime._responses["r1"].interrupted is True
    assert realtime._output_queue.qsize() == 0
    await realtime.close()


@pytest.mark.asyncio
async def test_idle_timeout_discards_silence_and_checks_the_caller_is_there() -> None:
    realtime = pipeline()
    realtime._running = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._handle_event(
        {
            "type": "input_audio_buffer.timeout_triggered",
            "item_id": "silence-1",
            "audio_start_ms": 3072,
            "audio_end_ms": 7776,
        }
    )

    # The committed silence must never become a caller turn.
    deletes = [event for event in sent if event["type"] == "conversation.item.delete"]
    assert [event["item_id"] for event in deletes] == ["silence-1"]
    assert "silence-1" in realtime._discarded_caller_items
    creates = [event for event in sent if event["type"] == "response.create"]
    assert len(creates) == 1
    assert creates[0]["response"]["metadata"]["phoneagent_kind"] == "reengage"
    await realtime.close()


@pytest.mark.asyncio
async def test_idle_reengagement_stops_after_the_limit_and_resets_on_speech() -> None:
    realtime = pipeline()
    realtime._running = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    async def idle(item_id: str) -> None:
        realtime._creating_kind = None
        realtime._generating_response_key = None
        await realtime._handle_event(
            {"type": "input_audio_buffer.timeout_triggered", "item_id": item_id}
        )

    for index in range(4):
        await idle(f"silence-{index}")
    creates = [event for event in sent if event["type"] == "response.create"]
    assert len(creates) == 2, "must not nag a silent caller indefinitely"

    # Real caller speech clears the streak.
    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )
    assert realtime._idle_reengagements == 0
    sent.clear()
    await idle("silence-9")
    assert sum(event["type"] == "response.create" for event in sent) == 1
    await realtime.close()


@pytest.mark.asyncio
async def test_idle_timeout_never_interrupts_a_speaking_agent() -> None:
    realtime = pipeline()
    realtime._running = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    await realtime._handle_event({"type": "response.created", "response": {"id": "r1"}})
    realtime._responses["r1"].first_audio_at = realtime._responses["r1"].created_at
    realtime._creating_kind = None
    realtime._generating_response_key = None
    sent.clear()

    await realtime._handle_event(
        {"type": "input_audio_buffer.timeout_triggered", "item_id": "silence-1"}
    )
    assert not any(event["type"] == "response.create" for event in sent)
    await realtime.close()


@pytest.mark.asyncio
async def test_reconnect_replays_both_sides_of_the_conversation() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime._connected.set()
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    realtime._record_turn("assistant", "Hello, this is Adam from OXzoon.")
    realtime._record_turn("user", "Yes, go ahead.")
    realtime._record_turn("assistant", "What do you watch most often?")

    assert realtime._replay_conversation() == 3
    replays = [event for event in sent if event["type"] == "conversation.item.create"]
    assert [event["item"]["role"] for event in replays] == ["assistant", "user", "assistant"]
    # Assistant turns replay as output_text; caller turns as input_text.
    assert replays[0]["item"]["content"][0]["type"] == "output_text"
    assert replays[1]["item"]["content"][0]["type"] == "input_text"
    assert replays[2]["item"]["content"][0]["text"] == "What do you watch most often?"
    await realtime.close()


@pytest.mark.asyncio
async def test_replayed_interrupted_turn_is_marked_as_cut_off() -> None:
    """Replaying an interrupted turn verbatim would re-create the divergence."""

    realtime = pipeline()
    realtime._record_turn("assistant", "Great. What do you watch most often?", interrupted=True)
    role, text = realtime._conversation_log[-1]
    assert role == "assistant"
    assert text.startswith("Great. What do you watch most often?")
    assert "cut off by the caller" in text
    await realtime.close()


@pytest.mark.asyncio
async def test_conversation_replay_log_is_bounded() -> None:
    realtime = pipeline()
    for index in range(CONVERSATION_REPLAY_TURNS * 2):
        realtime._record_turn("user", f"turn {index}")
    assert len(realtime._conversation_log) == CONVERSATION_REPLAY_TURNS
    # The most recent turns are the ones worth restoring.
    assert realtime._conversation_log[-1][1] == f"turn {CONVERSATION_REPLAY_TURNS * 2 - 1}"
    await realtime.close()


@pytest.mark.asyncio
async def test_a_dropped_caller_delete_keeps_the_turn_instead_of_ignoring_it() -> None:
    """A silently dropped delete used to mark the turn discarded anyway."""

    realtime = pipeline()
    realtime._running = True
    realtime.send_event = lambda _event: False  # type: ignore[method-assign]
    turn = realtime._caller_turn("caller-1")

    await realtime._discard_caller_turn(turn, "empty caller transcription")

    assert turn.discarded is False
    assert "caller-1" not in realtime._discarded_caller_items
    await realtime.close()


@pytest.mark.asyncio
async def test_the_model_owns_turn_taking_not_the_app() -> None:
    """VAD must be allowed to answer and to interrupt on the model's judgement."""

    realtime = pipeline()
    turn_detection = realtime._build_session_update()["session"]["audio"]["input"][
        "turn_detection"
    ]
    assert turn_detection["create_response"] is True
    assert turn_detection["interrupt_response"] is True


@pytest.mark.asyncio
async def test_app_never_creates_a_response_for_a_caller_turn() -> None:
    """A full caller turn must produce no client-side response.create at all."""

    realtime = pipeline()
    realtime._running = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )
    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "caller-1"}
    )
    await realtime._handle_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "caller-1",
            "transcript": "Yes, sports mostly.",
        }
    )

    assert not any(event["type"] == "response.create" for event in sent)
    await realtime.close()


@pytest.mark.asyncio
async def test_caller_turn_survives_an_empty_or_failed_transcription() -> None:
    """The model answers from audio; a lost side-transcript must not drop the turn."""

    realtime = pipeline()
    realtime._running = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )
    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "caller-1"}
    )
    await realtime._handle_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "caller-1",
            "transcript": "",
        }
    )
    await realtime._handle_event(
        {
            "type": "conversation.item.input_audio_transcription.failed",
            "item_id": "caller-2",
        }
    )

    # The turn used to be deleted out of the model's conversation here.
    assert not any(event["type"] == "conversation.item.delete" for event in sent)
    assert realtime._caller_turns["caller-1"].discarded is False
    await realtime.close()


@pytest.mark.asyncio
async def test_transcription_no_longer_rewrites_the_session_instructions() -> None:
    """Per-turn instruction injection competed with the model's own continuity."""

    realtime = pipeline()
    realtime._running = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._handle_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "caller-1",
            "transcript": "Yes, go ahead.",
        }
    )

    assert not any(event["type"] == "session.update" for event in sent)
    # The transcript is still observed for the operator view and caller memory.
    assert realtime._conversation_log[-1] == ("user", "Yes, go ahead.")
    await realtime.close()


@pytest.mark.asyncio
async def test_barge_in_still_flushes_the_phone_the_server_cannot_see() -> None:
    """Server-side interruption cannot know what Android already rendered."""

    realtime = pipeline()
    realtime._running = True
    realtime.transport.set_flush_handler(lambda _advance: {"status": "ok"})
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    await realtime._handle_event({"type": "response.created", "response": {"id": "r1"}})
    state = realtime._responses["r1"]
    state.output_item_id = "assistant-1"
    state.first_audio_at = state.created_at
    state.first_output_sequence = 0
    state.frames_written = 20
    state.audio_ms_generated = 400.0
    realtime.transport.session.metrics.last_rendered_sequence = 4

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )

    truncates = [event for event in sent if event["type"] == "conversation.item.truncate"]
    assert len(truncates) == 1
    assert truncates[0]["audio_end_ms"] == 100
    assert realtime.transport.session.generation_id == 2
    await realtime.close()


@pytest.mark.asyncio
async def test_tool_call_result_is_returned_and_the_answer_requested() -> None:
    realtime = pipeline()
    realtime._running = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._handle_event(
        {
            "type": "response.done",
            "response": {
                "id": "r1",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_abc",
                        "name": "callback_schedule",
                        "arguments": '{"when": "tomorrow morning"}',
                    }
                ],
            },
        }
    )

    outputs = [
        event
        for event in sent
        if event["type"] == "conversation.item.create"
        and event["item"]["type"] == "function_call_output"
    ]
    assert len(outputs) == 1
    assert outputs[0]["item"]["call_id"] == "call_abc"
    assert "noted_for_operator_confirmation" in outputs[0]["item"]["output"]
    # Turn detection only answers caller speech, so the continuation is explicit.
    creates = [event for event in sent if event["type"] == "response.create"]
    assert len(creates) == 1
    assert creates[0]["response"]["metadata"]["phoneagent_kind"] == "tool_result"
    await realtime.close()


@pytest.mark.asyncio
async def test_websocket_grounds_dictated_ticket_text_before_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realtime = pipeline()
    realtime._running = True
    realtime.policy.last_caller_text = (
        "Create a support ticket named Mac complete test saying the installation is working."
    )
    sent: list[dict] = []
    captured: list[str] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    async def fake_execute(_catalog, _name, arguments):
        captured.append(arguments)
        return '{"verified":true,"ticket_id":"test"}'

    monkeypatch.setattr(websocket_module, "execute_tool", fake_execute)
    await realtime._run_tool_calls(
        [
            (
                "ticket-1",
                "business_create_support_ticket",
                '{"subject":"My complete test","description":"Installation is working."}',
            )
        ]
    )

    assert json.loads(captured[0]) == {
        "subject": "Mac complete test",
        "description": "the installation is working.",
    }
    await realtime.close()


def test_tool_result_response_waits_until_an_active_response_finishes() -> None:
    realtime = pipeline()
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    realtime._generating_response_key = "caller-response"
    realtime._response_idle.clear()
    realtime._pending_tool_response = True

    realtime._dispatch_pending_tool_response()

    assert not any(event["type"] == "response.create" for event in sent)
    realtime._generating_response_key = None
    realtime._response_idle.set()
    realtime._dispatch_pending_tool_response()
    creates = [event for event in sent if event["type"] == "response.create"]
    assert len(creates) == 1
    assert creates[0]["response"]["metadata"]["phoneagent_kind"] == "tool_result"


@pytest.mark.asyncio
async def test_the_session_advertises_exactly_the_registered_tools() -> None:
    realtime = pipeline()
    session = realtime._build_session_update()["session"]
    assert session["tool_choice"] == "auto"
    assert [tool["name"] for tool in session["tools"]] == list(realtime.tool_catalog)
    # The persona names the same tools it can actually call.
    assert realtime.policy.available_tools == set(realtime.tool_catalog)
    assert "Current caller phone number: +33123456789" in session["instructions"]
    assert "Never announce it unsolicited" in session["instructions"]
    await realtime.close()


@pytest.mark.asyncio
async def test_managed_tool_activation_hot_updates_the_live_realtime_session(
    tmp_path: Path,
) -> None:
    store = ToolControlStore(tmp_path / "tool-control.json")
    tool = ManagedToolPolicy(
        source_name="internet_search",
        exposed_name="internet_search",
        description="Search current public information after announcing a short wait.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 200}},
            "required": ["query"],
            "additionalProperties": False,
        },
        enabled=True,
        task_ids=["iptv_subscription_sales"],
        read_only=True,
    )
    connection = ToolConnection(
        id="search_service",
        label="Search",
        kind="http",
        enabled=True,
        url="https://search.example/api",
        tools=[tool],
    )
    store.save(ToolControlConfig(connections=[connection]).model_dump(mode="json"))

    realtime = pipeline()
    realtime.tool_control_store = store
    realtime._running = True
    realtime._connected.set()
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._reload_managed_tools(update_session=True)

    assert "internet_search" in realtime.tool_catalog
    update = next(event for event in sent if event["type"] == "session.update")
    assert "internet_search" in [item["name"] for item in update["session"]["tools"]]
    assert "few seconds" in update["session"]["instructions"]
    await realtime.close()


@pytest.mark.asyncio
async def test_openwa_tool_activation_hot_updates_current_live_call(tmp_path: Path) -> None:
    store = OpenWAConfigStore(tmp_path / "openwa.json")
    draft = OpenWAConfig(
        enabled=True,
        api_key="test-key",
        session_id="session-123",
        live_events_enabled=False,
    )
    policies = [
        policy.model_copy(
            update={"enabled": policy.name == "whatsapp_send_text_current_customer"}
        )
        for policy in draft.tools
    ]
    store.save(draft.model_copy(update={"tools": policies}).model_dump(mode="json"))
    realtime = pipeline()
    realtime.openwa_config_store = store
    realtime._running = True
    realtime._connected.set()
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._reload_openwa(update_session=True)

    assert "whatsapp_send_text_current_customer" in realtime.tool_catalog
    update = next(event for event in sent if event["type"] == "session.update")
    assert "whatsapp_send_text_current_customer" in [
        tool["name"] for tool in update["session"]["tools"]
    ]
    assert "current caller" in update["session"]["instructions"]
    await realtime.close()


@pytest.mark.asyncio
async def test_web_research_hot_updates_current_call_without_touching_media(
    tmp_path: Path,
) -> None:
    store = WebResearchConfigStore(tmp_path / "web-research.json")
    store.save(
        WebResearchConfig(enabled=True, task_ids=["iptv_subscription_sales"]).model_dump(
            mode="json"
        )
    )
    realtime = pipeline()
    realtime.web_research_config_store = store
    realtime._running = True
    realtime._connected.set()
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._reload_web_research(update_session=True)

    assert "web_research" in realtime.tool_catalog
    update = next(event for event in sent if event["type"] == "session.update")
    assert "web_research" in [item["name"] for item in update["session"]["tools"]]
    assert "few seconds" in update["session"]["instructions"]
    assert "does not judge relevance" in update["session"]["instructions"]
    assert "at most three materially different searches" in update["session"]["instructions"]
    assert realtime.input_bridge is None
    await realtime.close()


@pytest.mark.asyncio
async def test_reconnect_answers_the_outstanding_turn_without_asking_for_a_repeat() -> None:
    """Replaying the history and asking the caller to repeat contradict each other."""

    realtime = pipeline()
    realtime._running = True
    realtime._connected.set()
    realtime._greeted = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    realtime._record_turn("assistant", "Hello, this is Adam from OXzoon.")
    realtime._record_turn("user", "Yes, go ahead.")

    realtime._resume_after_reconnect(replayed=2)

    creates = [event for event in sent if event["type"] == "response.create"]
    assert len(creates) == 1
    instructions = creates[0]["response"]["instructions"]
    assert "Answer it now" in instructions
    assert "repeat" in instructions and "Do not ask them to repeat" in instructions
    await realtime.close()


@pytest.mark.asyncio
async def test_reconnect_stays_silent_when_it_is_the_callers_turn() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime._connected.set()
    realtime._greeted = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    realtime._record_turn("user", "Yes, go ahead.")
    realtime._record_turn("assistant", "Great. What do you watch most often?")

    realtime._resume_after_reconnect(replayed=2)

    # Speaking here would talk over a caller who is already answering.
    assert not any(event["type"] == "response.create" for event in sent)
    await realtime.close()


@pytest.mark.asyncio
async def test_reconnect_asks_for_a_repeat_only_when_nothing_could_be_restored() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime._connected.set()
    realtime._greeted = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    realtime._resume_after_reconnect(replayed=0)

    creates = [event for event in sent if event["type"] == "response.create"]
    assert len(creates) == 1
    assert "repeat" in creates[0]["response"]["instructions"]
    await realtime.close()


@pytest.mark.asyncio
async def test_reconnect_resumes_an_assistant_turn_the_line_cut_short() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime._connected.set()
    realtime._greeted = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    realtime._record_turn("user", "Yes, go ahead.")
    realtime._record_turn(
        "assistant",
        "Great. What do you watch",
        interrupted=True,
        interrupted_by="connection",
    )

    realtime._resume_after_reconnect(replayed=2)

    creates = [event for event in sent if event["type"] == "response.create"]
    assert len(creates) == 1
    assert "Finish only the thought" in creates[0]["response"]["instructions"]
    await realtime.close()


@pytest.mark.asyncio
async def test_a_dropped_line_is_not_recorded_as_a_caller_interruption() -> None:
    realtime = pipeline()
    realtime._record_turn(
        "assistant", "Great. What do you watch", interrupted=True, interrupted_by="connection"
    )
    _, text = realtime._conversation_log[-1]
    assert "line dropped" in text
    assert "cut off by the caller" not in text
    await realtime.close()


@pytest.mark.asyncio
async def test_quiet_phone_audio_is_preserved_and_batched_at_exact_duration() -> None:
    bridge = _PhoneInputBridge(asyncio.get_running_loop())
    bridge.enable()
    t = np.arange(320) / 16_000
    quiet = (np.sin(2 * np.pi * 440 * t) * 35).astype("<i2").tobytes()

    for _ in range(2):
        bridge.push_pcm_frame(quiet)

    batch = bridge.queue.get_nowait()
    assert (
        REALTIME_SAMPLE_RATE * 30 // 1000 * 2
        <= len(batch)
        <= REALTIME_SAMPLE_RATE * 40 // 1000 * 2
    )
    assert np.max(np.abs(np.frombuffer(batch, dtype="<i2"))) > 10
    assert bridge.quality_snapshot()["caller_input_queue_drops"] == 0
    bridge.stop()


def test_startup_verifier_rejects_transient_without_modifying_audio_path() -> None:
    verifier = _StartupSpeechVerifier()
    silence = b"\x00" * PHONE_FRAME_BYTES
    click_samples = np.zeros(320, dtype="<i2")
    click_samples[0] = 5_136
    click = click_samples.tobytes()

    verifier.observe(click)
    for _ in range(STARTUP_STABILIZER_MAX_FRAMES - 1):
        verifier.observe(silence)

    assert verifier.snapshot() == {
        "startup_verifier_observed_frames": STARTUP_STABILIZER_MAX_FRAMES,
        "startup_verifier_human_confirmed": False,
        "startup_verifier_settled": True,
    }
    ordinary = np.full(320, 25, dtype="<i2").tobytes()
    verifier.observe(ordinary)
    assert verifier.human_confirmed is False


def test_startup_verifier_confirms_genuine_sustained_speech() -> None:
    verifier = _StartupSpeechVerifier()
    t = np.arange(320) / 16_000
    frames = [
        (np.sin(2 * np.pi * (220 + index) * t) * 1_000).astype("<i2").tobytes()
        for index in range(STARTUP_STABILIZER_MIN_FRAMES)
    ]
    for frame in frames:
        verifier.observe(frame)

    snapshot = verifier.snapshot()
    assert snapshot["startup_verifier_human_confirmed"] is True


@pytest.mark.asyncio
async def test_semantic_vad_remains_an_explicit_compatibility_option() -> None:
    config = runtime_config()
    config = replace(
        config,
        providers=replace(
            config.providers,
            chatgpt_realtime_vad_mode="semantic_vad",
            chatgpt_realtime_vad_eagerness="high",
        ),
    )
    realtime = OpenAIRealtimeWebSocketPipeline(
        active_transport(), config, auth_manager=MagicMock(), caller_id="test"
    )

    turn_detection = realtime._build_session_update()["session"]["audio"]["input"][
        "turn_detection"
    ]

    assert turn_detection == {
        "type": "semantic_vad",
        "eagerness": "high",
        "create_response": True,
        "interrupt_response": True,
    }
    await realtime.close()


@pytest.mark.asyncio
async def test_output_audio_done_flushes_every_sample_before_end_marker() -> None:
    realtime = pipeline()
    realtime._running = True
    await realtime._handle_event(
        {"type": "response.created", "response": {"id": "response-1"}}
    )
    t = np.arange(REALTIME_SAMPLE_RATE // 10) / REALTIME_SAMPLE_RATE
    source = (np.sin(2 * np.pi * 440 * t) * 10_000).astype("<i2").tobytes()

    await realtime._handle_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "response-1",
            "item_id": "assistant-1",
            "content_index": 0,
            "delta": base64.b64encode(source).decode("ascii"),
        }
    )
    await realtime._handle_event(
        {
            "type": "response.output_audio.done",
            "response_id": "response-1",
            "item_id": "assistant-1",
            "content_index": 0,
        }
    )

    items = []
    while not realtime._output_queue.empty():
        items.append(realtime._output_queue.get_nowait())
    assert items[-1].pcm is None
    chunks = [item.pcm for item in items[:-1]]
    assert chunks and all(len(chunk) == PHONE_FRAME_BYTES for chunk in chunks)
    assert len(b"".join(chunk for chunk in chunks if chunk)) == 5 * PHONE_FRAME_BYTES
    await realtime.close()


@pytest.mark.asyncio
async def test_barge_in_flushes_phone_and_sends_exactly_one_truncate() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime.transport.set_flush_handler(lambda _advance: {"status": "ok"})
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    await realtime._handle_event(
        {"type": "response.created", "response": {"id": "response-1"}}
    )
    state = realtime._responses["response-1"]
    state.output_item_id = "assistant-1"
    state.first_audio_at = state.created_at
    # Twenty frames (400 ms) were written for this response starting at the
    # session's first output sequence; Android rendered sequences 0..4.
    state.first_output_sequence = 0
    state.frames_written = 20
    state.audio_ms_generated = 400.0
    realtime.transport.session.metrics.last_rendered_sequence = 4

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )
    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )

    truncates = [event for event in sent if event["type"] == "conversation.item.truncate"]
    assert len(truncates) == 1
    assert truncates[0]["item_id"] == "assistant-1"
    assert truncates[0]["audio_end_ms"] == 100
    # turn_detection.interrupt_response stops generation server side; a second
    # client cancel here would race the response the server creates next.
    assert not any(event["type"] == "response.cancel" for event in sent)
    assert not any(event["type"] == "output_audio_buffer.clear" for event in sent)
    assert realtime.transport.session.generation_id == 2
    await realtime.close()


@pytest.mark.asyncio
async def test_truncate_stays_exact_across_consecutive_barge_ins() -> None:
    """A flush discards frames that already consumed global sequence numbers.

    Counting delivered audio by differencing the call-global output sequence
    made every truncate after the first barge-in over-report by the number of
    discarded frames. The API rejects an audio_end_ms longer than the item
    ("Audio content of Nms is already shorter than Mms"), so the truncate never
    applied and the model kept believing it had spoken the whole interrupted
    turn.
    """

    realtime = pipeline()
    realtime._running = True
    realtime.transport.set_flush_handler(lambda _advance: {"status": "ok"})
    realtime.transport.set_tx_handler(lambda *_args: None)
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    session = realtime.transport.session

    async def play(response_key: str, frames: int) -> None:
        """Write `frames` of this response to the phone and render them all."""
        for _ in range(frames):
            await realtime._output_queue.put(
                _OutputQueueItem(response_key=response_key, pcm=b"\x00" * PHONE_FRAME_BYTES)
            )
        for _ in range(frames):
            item = await realtime._output_queue.get()
            state = realtime._responses[item.response_key]
            result = await realtime.transport.output().write_audio_frame_result(
                OutputAudioRawFrame(audio=item.pcm, sample_rate=16_000, num_channels=1)
            )
            assert result is AudioWriteResult.DELIVERED
            if state.first_output_sequence is None:
                state.first_output_sequence = session.metrics.last_output_sequence
            state.frames_written += 1
            realtime._output_queue.task_done()

    # --- First response: 20 frames written, only 5 reach the caller ---
    await realtime._handle_event({"type": "response.created", "response": {"id": "r1"}})
    first = realtime._responses["r1"]
    first.output_item_id = "assistant-1"
    first.first_audio_at = first.created_at
    first.audio_ms_generated = 400.0
    await play("r1", 20)
    session.mark_rendered(session.generation_id, 4)

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )
    truncate = [event for event in sent if event["type"] == "conversation.item.truncate"][-1]
    assert truncate["audio_end_ms"] == 100

    # --- Second response: the caller hears exactly one 20 ms frame ---
    # Fifteen frames of the first response were flushed after consuming
    # sequence numbers. They must not count as delivered here.
    sent.clear()
    await realtime._handle_event({"type": "response.created", "response": {"id": "r2"}})
    second = realtime._responses["r2"]
    second.output_item_id = "assistant-2"
    second.first_audio_at = second.created_at
    second.audio_ms_generated = 400.0
    await play("r2", 20)
    session.mark_rendered(session.generation_id, second.first_output_sequence)

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-2"}
    )
    truncate = [event for event in sent if event["type"] == "conversation.item.truncate"][-1]
    assert truncate["audio_end_ms"] == 20, (
        "truncate must report only what this response delivered, not frames "
        "discarded by the previous barge-in"
    )
    await realtime.close()


@pytest.mark.asyncio
async def test_truncate_never_exceeds_the_audio_the_model_generated() -> None:
    """audio_end_ms longer than the item is rejected, leaving the turn intact."""

    realtime = pipeline()
    realtime._running = True
    realtime.transport.set_flush_handler(lambda _advance: {"status": "ok"})
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    await realtime._handle_event({"type": "response.created", "response": {"id": "r1"}})
    state = realtime._responses["r1"]
    state.output_item_id = "assistant-1"
    state.first_audio_at = state.created_at
    # Playout accounting runs ahead of what the model actually produced.
    state.first_output_sequence = 0
    state.frames_written = 50
    state.audio_ms_generated = 240.0
    realtime.transport.session.metrics.last_rendered_sequence = 49

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )
    truncate = [event for event in sent if event["type"] == "conversation.item.truncate"][-1]
    assert truncate["audio_end_ms"] == 240
    await realtime.close()


@pytest.mark.asyncio
async def test_late_output_identity_still_gets_one_pending_truncate() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime.transport.set_flush_handler(lambda _advance: {"status": "ok"})
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    await realtime._handle_event(
        {"type": "response.created", "response": {"id": "response-1"}}
    )
    state = realtime._responses["response-1"]
    state.first_audio_at = state.created_at

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )
    # Nothing can be truncated yet: the assistant item has no id.
    assert sent == []

    await realtime._handle_event(
        {
            "type": "response.output_item.added",
            "response_id": "response-1",
            "item": {"id": "assistant-1", "type": "message"},
        }
    )
    truncates = [event for event in sent if event["type"] == "conversation.item.truncate"]
    assert len(truncates) == 1
    assert truncates[0]["audio_end_ms"] == 0
    await realtime.close()


@pytest.mark.asyncio
async def test_response_remains_interruptible_until_android_finishes_playout() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime.transport.set_flush_handler(lambda _advance: {"status": "ok"})
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    await realtime._handle_event(
        {"type": "response.created", "response": {"id": "response-1"}}
    )
    state = realtime._responses["response-1"]
    state.output_item_id = "assistant-1"
    state.first_audio_at = state.created_at
    state.text = "This has finished generating but is still playing on the phone."

    await realtime._handle_event(
        {"type": "response.done", "response": {"id": "response-1", "output": []}}
    )
    assert realtime._active_response_key == "response-1"

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "caller-1"}
    )

    assert any(event["type"] == "conversation.item.truncate" for event in sent)
    assert realtime.transport.session.generation_id == 2
    assert realtime._active_response_key is None
    await realtime.close()


@pytest.mark.asyncio
async def test_native_spoken_transcript_is_not_rewritten_after_audio_played() -> None:
    events: list[dict] = []
    realtime = pipeline(events=events)
    await realtime.policy.finalize_response(
        "Hello, this is Adam from OXzoon. Is now a good time?",
        response_kind="greeting",
    )
    state = realtime._response_state({"response_id": "response-2"})
    state.text = "Hello, this is Adam from OXzoon. Is now a good time?"

    await realtime._finalize_response(state)

    assistant = [event for event in events if event.get("role") == "assistant"]
    assert assistant[-1]["text"] == state.text
    await realtime.close()


@pytest.mark.asyncio
async def test_side_transcript_does_not_decide_call_completion_for_the_ai() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime._connected.set()
    realtime.transport.set_flush_handler(lambda _advance: {"status": "ok"})
    realtime.policy._opening_attempted = True
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._handle_event(
        {"type": "response.created", "response": {"id": "sales-response"}}
    )
    await realtime._handle_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "caller-refusal",
            "transcript": "No.",
            "language": "en",
            "logprobs": [{"logprob": -0.01}],
        }
    )

    # The model heard the original audio and owns the end_call decision. The
    # side transcript may update live state, but no regex may cancel or hang up.
    assert sent == []
    assert realtime._responses["sales-response"].suppress_transcript is False
    assert realtime.policy._last_caller_intent == "permission_refused"
    await realtime.close()


@pytest.mark.asyncio
async def test_ai_end_call_tool_queues_one_exact_terminal_closing() -> None:
    events: list[dict] = []
    realtime = pipeline(events=events)
    realtime._running = True
    realtime._connected.set()
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._handle_event(
        {"type": "response.created", "response": {"id": "decision-response"}}
    )
    await realtime._handle_event(
        {
            "type": "response.done",
            "response": {
                "id": "decision-response",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call-end-1",
                        "name": "end_call",
                        "arguments": json.dumps(
                            {
                                "reason": "The caller said goodbye after receiving help.",
                                "closing_message": "You're welcome. Goodbye and have a great day.",
                            }
                        ),
                    }
                ],
            },
        }
    )

    outputs = [
        event
        for event in sent
        if event["type"] == "conversation.item.create"
        and event["item"]["type"] == "function_call_output"
    ]
    assert len(outputs) == 1
    assert json.loads(outputs[0]["item"]["output"])["accepted"] is True
    terminal = [
        event
        for event in sent
        if event["type"] == "response.create"
        and event["response"]["metadata"] == {"phoneagent_kind": "terminal"}
    ]
    assert len(terminal) == 1
    assert terminal[0]["response"]["tool_choice"] == "none"
    assert "You're welcome. Goodbye and have a great day." in terminal[0]["response"][
        "instructions"
    ]
    assert sum(
        event["type"] == "session.update"
        and event["event_id"].startswith("phoneagent_terminal_vad_guard_")
        for event in sent
    ) == 1
    assert not any(
        event["type"] == "response.create"
        and event["response"]["metadata"] == {"phoneagent_kind": "tool_result"}
        for event in sent
    )
    assert [event for event in events if event.get("type") == "ai_end_call_requested"]
    await realtime.close()


@pytest.mark.asyncio
async def test_ai_end_call_cannot_strand_call_when_closing_audio_is_missing() -> None:
    events: list[dict] = []
    realtime = pipeline(events=events)
    realtime._running = True
    realtime._connected.set()
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]

    await realtime._accept_ai_end_call(
        {
            "accepted": True,
            "reason": "conversation complete",
            "closing_message": "Goodbye.",
        },
        source_state=None,
    )
    for response_id in ("terminal-empty-1", "terminal-empty-2"):
        await realtime._handle_event(
            {
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "metadata": {"phoneagent_kind": "terminal"},
                },
            }
        )
        await realtime._handle_event(
            {
                "type": "response.done",
                "response": {"id": response_id, "status": "completed", "output": []},
            }
        )

    terminal_creates = [
        event
        for event in sent
        if event["type"] == "response.create"
        and event["response"]["metadata"] == {"phoneagent_kind": "terminal"}
    ]
    assert len(terminal_creates) == 2
    assert realtime._terminal_completion_notified is True
    completions = [event for event in events if event.get("type") == "call_completion"]
    assert len(completions) == 1
    assert "without audio" in completions[0]["reason"]
    await realtime.close()


@pytest.mark.asyncio
async def test_terminal_closing_overlap_cannot_create_a_second_goodbye_turn() -> None:
    realtime = pipeline()
    realtime._running = True
    realtime._connected.set()
    sent: list[dict] = []
    realtime.send_event = record_events(sent)  # type: ignore[method-assign]
    await realtime._handle_event(
        {
            "type": "response.created",
            "response": {
                "id": "terminal-response",
                "metadata": {"phoneagent_kind": "terminal"},
            },
        }
    )
    state = realtime._responses["terminal-response"]
    state.first_audio_at = state.created_at

    await realtime._handle_event(
        {"type": "input_audio_buffer.speech_started", "item_id": "farewell-overlap"}
    )

    assert state.interrupted is False
    assert not any(event["type"] == "response.cancel" for event in sent)
    assert not any(event["type"] == "conversation.item.truncate" for event in sent)
    await realtime.close()


@pytest.mark.asyncio
async def test_terminal_playout_hangs_up_exactly_once() -> None:
    completions: list[str] = []

    async def complete(reason: str) -> None:
        completions.append(reason)

    realtime = OpenAIRealtimeWebSocketPipeline(
        active_transport(),
        runtime_config(),
        auth_manager=MagicMock(),
        caller_id="+33123456789",
        call_completion_sink=complete,
    )
    realtime._running = True
    realtime._ai_end_call_reason = "caller completed the conversation"
    state = realtime._response_state({"response_id": "terminal-response"}, kind="terminal")
    state.response_status = "completed"
    state.first_output_sequence = 1
    state.frames_written = 3
    state.audio_end = (realtime.transport.session.generation_id, 3)
    state.finalized.set()
    realtime.transport.session.metrics.last_rendered_sequence = 3

    await realtime._monitor_playback(state)
    await realtime._notify_terminal_completion("duplicate completion")

    assert completions == ["AI ended call: caller completed the conversation"]
    await realtime.close()


@pytest.mark.asyncio
async def test_incomplete_response_is_reported_as_failed_not_completed() -> None:
    events: list[dict] = []
    realtime = pipeline(events=events)
    realtime._running = True
    await realtime._handle_event(
        {"type": "response.created", "response": {"id": "limited-response"}}
    )
    await realtime._handle_event(
        {
            "type": "response.output_audio_transcript.done",
            "response_id": "limited-response",
            "transcript": "This sentence was cut because",
        }
    )
    await realtime._handle_event(
        {
            "type": "response.done",
            "response": {
                "id": "limited-response",
                "status": "incomplete",
                "status_details": {"type": "incomplete", "reason": "max_output_tokens"},
                "output": [],
                "usage": {
                    "output_tokens": 128,
                    "output_token_details": {"audio_tokens": 96},
                },
            },
        }
    )

    state = realtime._responses["limited-response"]
    assert state.response_status == "incomplete"
    assert state.output_tokens == 128
    assert state.audio_output_tokens == 96
    statuses = [event for event in events if event.get("type") == "playback_status"]
    assert statuses[-1]["status"] == "failed"
    assert "max_output_tokens" in statuses[-1]["message"]
    await realtime.close()
