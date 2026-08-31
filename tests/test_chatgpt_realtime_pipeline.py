"""Unit tests for ChatGPT Realtime WebRTC pipeline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import phone_agent_gateway.ai_bridge.chatgpt_realtime_pipeline as webrtc_module
from phone_agent_gateway.ai_bridge.chatgpt_realtime_pipeline import (
    CALLER_TURN_SETTLE_SECS,
    ChatGPTRealtimePipeline,
)
from phone_agent_gateway.ai_bridge.openwa_integration import OpenWAConfig, OpenWAConfigStore
from phone_agent_gateway.ai_bridge.pipecat_transport import PhoneAgentTransport
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig, RuntimeConfig
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


@pytest.fixture
def mock_runtime_config():
    providers = ProviderConfig(
        pipeline_mode="s2s_chatgpt_realtime",
        chatgpt_realtime_voice="alloy",
        stt_language="en-US",
    )
    return RuntimeConfig(
        device_id="test_device",
        control_host="127.0.0.1",
        control_port=8765,
        protocol_control_port=8768,
        rx_port=8766,
        tx_port=8767,
        sample_rate=16000,
        frame_ms=20,
        input_queue_frames=25,
        auto_answer=False,
        record_calls=False,
        memory_enabled=False,
        task_id="iptv_subscription_sales",
        event_stream_enabled=True,
        voice_lock_path=MagicMock(),
        system_prompt="Test instructions",
        link_authentication_key=b"0" * 32,
        providers=providers,
    )


@pytest.fixture
def transport_session():
    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    session.set_phase(SessionPhase.ACTIVE)
    transport = PhoneAgentTransport(session=session)
    return transport


@pytest.mark.asyncio
async def test_chatgpt_realtime_pipeline_lifecycle(mock_runtime_config, transport_session):
    mock_auth = MagicMock()
    mock_auth.get_token.return_value = "dummy_token"

    emitted_events = []

    def sink(event):
        emitted_events.append(event)

    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=mock_auth,
        caller_id="+33123456789",
        event_sink=sink,
    )

    assert pipeline.voice == "alloy"
    assert pipeline.caller_id == "+33123456789"
    assert pipeline.policy is not None
    # Assert the grounding contract, not one phrasing of it. compile() was
    # reworded in 8378a6a ("Active Caller Phone Number", plus guidance to state
    # the number when asked) while compile_realtime() kept the older sentence,
    # so pinning the exact string made this test track wording rather than
    # behaviour. What must hold is that the number is grounded and the model is
    # told not to ask the caller for it.
    prompt = pipeline.policy.system_prompt
    assert "+33123456789" in prompt
    assert "NEVER ask the caller to provide" in prompt

    # Test closing
    await pipeline.close()
    assert pipeline._closed


@pytest.mark.asyncio
async def test_webrtc_managed_tools_hot_update_without_restarting_media(
    mock_runtime_config,
    transport_session,
    tmp_path: Path,
) -> None:
    store = ToolControlStore(tmp_path / "tool-control.json")
    policy = ManagedToolPolicy(
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
        tools=[policy],
    )
    store.save(ToolControlConfig(connections=[connection]).model_dump(mode="json"))
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    pipeline.tool_control_store = store
    pipeline._running = True
    pipeline.dc = MagicMock(readyState="open")

    await pipeline._reload_managed_tools(update_session=True)

    sent = json.loads(pipeline.dc.send.call_args.args[0])
    assert sent["type"] == "session.update"
    assert "internet_search" in [tool["name"] for tool in sent["session"]["tools"]]
    assert pipeline.pc is None  # Tool activation never replaced or touched WebRTC media.
    await pipeline.close()


@pytest.mark.asyncio
async def test_webrtc_openwa_tools_hot_update_without_touching_media(
    mock_runtime_config,
    transport_session,
    tmp_path: Path,
) -> None:
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
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    pipeline.openwa_config_store = store
    pipeline._running = True
    pipeline.dc = MagicMock(readyState="open")

    await pipeline._reload_openwa(update_session=True)

    sent = json.loads(pipeline.dc.send.call_args.args[0])
    assert "whatsapp_send_text_current_customer" in [
        tool["name"] for tool in sent["session"]["tools"]
    ]
    assert pipeline.pc is None
    await pipeline.close()


@pytest.mark.asyncio
async def test_webrtc_web_research_hot_update_without_touching_media(
    mock_runtime_config,
    transport_session,
    tmp_path: Path,
) -> None:
    store = WebResearchConfigStore(tmp_path / "web-research.json")
    store.save(
        WebResearchConfig(enabled=True, task_ids=["iptv_subscription_sales"]).model_dump(
            mode="json"
        )
    )
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    pipeline.web_research_config_store = store
    pipeline._running = True
    pipeline.dc = MagicMock(readyState="open")

    await pipeline._reload_web_research(update_session=True)

    sent = json.loads(pipeline.dc.send.call_args.args[0])
    assert "web_research" in [tool["name"] for tool in sent["session"]["tools"]]
    assert "few seconds" in sent["session"]["instructions"]
    assert "does not judge relevance" in sent["session"]["instructions"]
    assert "at most three materially different searches" in sent["session"]["instructions"]
    assert pipeline.pc is None
    await pipeline.close()


@pytest.mark.asyncio
async def test_realtime_session_is_bilingual_and_preserves_carrier_audio(
    mock_runtime_config, transport_session
):
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )

    update = pipeline._build_session_update()
    session = update["session"]
    audio_input = session["audio"]["input"]
    transcription = audio_input["transcription"]

    assert session["include"] == ["item.input_audio_transcription.logprobs"]
    assert transcription["model"] == "gpt-live-transcribe"
    assert transcription["languages"] == ["en", "fr"]
    assert "language" not in transcription
    assert "abonnement" in transcription["keywords"]
    assert audio_input["noise_reduction"] is None
    assert audio_input["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "medium",
        "create_response": False,
        "interrupt_response": False,
    }
    assert "BILINGUAL CALL LANGUAGE" in session["instructions"]
    assert "STRICT LANGUAGE DIRECTIVE" not in session["instructions"]
    assert any(tool["name"] == "end_call" for tool in session["tools"])
    assert "You own the conversational decision" in session["instructions"]
    await pipeline.close()


@pytest.mark.asyncio
async def test_webrtc_ai_end_call_uses_terminal_response_then_one_completion(
    mock_runtime_config, transport_session
) -> None:
    completions: list[str] = []

    async def complete(reason: str) -> None:
        completions.append(reason)

    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
        call_completion_sink=complete,
    )
    pipeline.dc = MagicMock(readyState="open")

    await pipeline._run_tool_calls(
        [
            (
                "end-1",
                "end_call",
                json.dumps(
                    {
                        "reason": "Caller said goodbye.",
                        "closing_message": "Thank you for calling. Goodbye.",
                    }
                ),
            )
        ]
    )

    sent = [json.loads(call.args[0]) for call in pipeline.dc.send.call_args_list]
    terminal = [event for event in sent if event["type"] == "response.create"]
    assert len(terminal) == 1
    assert terminal[0]["response"]["metadata"] == {"phoneagent_kind": "terminal"}
    assert terminal[0]["response"]["tool_choice"] == "none"
    assert "Thank you for calling. Goodbye." in terminal[0]["response"]["instructions"]

    pipeline._running = True
    state = pipeline._response_state({}, {"response_id": "terminal-1"}, kind="terminal")
    state.audio_end = (transport_session.session.generation_id, 3)
    state.finalized.set()
    transport_session.session.metrics.last_rendered_sequence = 3
    await pipeline._monitor_phone_playback(state)
    await pipeline._notify_terminal_completion("duplicate")

    assert completions == ["AI ended call: Caller said goodbye."]
    await pipeline.close()


@pytest.mark.asyncio
async def test_webrtc_grounds_dictated_ticket_text_before_tool_execution(
    mock_runtime_config,
    transport_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    pipeline.dc = MagicMock(readyState="open")
    pipeline.policy.last_caller_text = (
        "Create a support ticket named Mac complete test saying the installation is working."
    )
    captured: list[str] = []

    async def fake_execute(_catalog, _name, arguments):
        captured.append(arguments)
        return '{"verified":true,"ticket_id":"test"}'

    monkeypatch.setattr(webrtc_module, "execute_tool", fake_execute)
    await pipeline._run_tool_calls(
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
    await pipeline.close()


@pytest.mark.asyncio
async def test_datachannel_interruption_event(mock_runtime_config, transport_session):
    mock_auth = MagicMock()
    mock_auth.get_token.return_value = "dummy_token"

    flush_mock = AsyncMock(return_value={"status": "ok"})
    transport_session.set_flush_handler(flush_mock)

    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=mock_auth,
        caller_id="+33123456789",
    )

    # A speech-start edge or one stray word must not cut off the assistant.
    pipeline._assistant_is_speaking = True
    initial_gen = transport_session.session.generation_id
    msg = json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "brief-item"})
    await pipeline._handle_dc_message(msg)
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "brief-item",
                "transcript": "you",
            }
        )
    )
    assert transport_session.session.generation_id == initial_gen
    assert not flush_mock.called
    assert pipeline._assistant_is_speaking is True

    # A completed meaningful barge-in is admitted and interrupts atomically.
    await pipeline._handle_dc_message(
        json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "actionable-item"})
    )
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "actionable-item",
                "transcript": "Actually, stop for a moment.",
            }
        )
    )
    assert transport_session.session.generation_id == initial_gen + 1
    assert flush_mock.called
    assert pipeline._assistant_is_speaking is False

    await pipeline.close()


@pytest.mark.asyncio
async def test_datachannel_transcript_events(mock_runtime_config, transport_session):
    mock_auth = MagicMock()
    mock_auth.get_token.return_value = "dummy_token"

    emitted_events = []

    def sink(event):
        emitted_events.append(event)

    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=mock_auth,
        caller_id="+33123456789",
        event_sink=sink,
    )
    sent_events = []
    pipeline.send_event = MagicMock(side_effect=sent_events.append)
    pipeline._session_instructions = "Exact persona and task"

    # 1. Caller transcript completed
    user_msg = json.dumps(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "Hello, I want IPTV for football.",
        }
    )
    await pipeline._handle_dc_message(user_msg)
    await asyncio.sleep(CALLER_TURN_SETTLE_SECS + 0.05)
    assert pipeline.policy.last_caller_text == "Hello, I want IPTV for football."
    assert [event["type"] for event in sent_events] == ["response.create"]
    assert "active task" in sent_events[0]["response"]["instructions"]
    assert sent_events[0]["event_id"].startswith("phoneagent_response_create_")
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "response.created",
                "response": {"id": "resp-transcript", "status": "in_progress"},
            }
        )
    )

    # 2. Assistant delta
    delta_msg = json.dumps(
        {
            "type": "response.audio_transcript.delta",
            "response_id": "resp-transcript",
            "delta": "We offer ",
        }
    )
    await pipeline._handle_dc_message(delta_msg)
    assert pipeline._current_assistant_text == "We offer "
    assert any(
        ev.get("type") == "transcript_delta" and ev.get("delta") == "We offer "
        for ev in emitted_events
    )

    # 3. Assistant completion
    done_msg = json.dumps(
        {
            "type": "response.audio_transcript.done",
            "response_id": "resp-transcript",
        }
    )
    await pipeline._handle_dc_message(done_msg)
    assert pipeline._current_assistant_text == ""
    assert pipeline.policy.last_spoken_turn() == "We offer"

    await pipeline.close()


@pytest.mark.asyncio
async def test_chatgpt_realtime_clean_slate_greeting_directive(
    mock_runtime_config, transport_session
):
    mock_auth = MagicMock()
    mock_auth.get_token.return_value = "dummy_token"

    sent_events = []

    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=mock_auth,
        caller_id="+33123456789",
    )

    pipeline.send_event = MagicMock(side_effect=lambda ev: sent_events.append(ev))
    media_track = MagicMock()
    pipeline.media_track = media_track

    await pipeline.greet()
    assert pipeline._greeted is True
    assert len(sent_events) == 1
    assert sent_events[0].get("type") == "response.create"
    assert "instructions" in sent_events[0].get("response", {})
    media_track.enable_input.assert_called_once_with()

    await pipeline.close()


@pytest.mark.asyncio
async def test_goodbye_turn_requests_only_a_brief_goodbye(mock_runtime_config, transport_session):
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    pipeline.policy._opening_attempted = True
    sent_events = []
    pipeline.send_event = MagicMock(side_effect=sent_events.append)
    pipeline._session_instructions = "Exact persona and task"

    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Bye.",
            }
        )
    )
    await asyncio.sleep(CALLER_TURN_SETTLE_SECS + 0.05)

    assert [event["type"] for event in sent_events] == ["response.create"]
    instruction = sent_events[-1]["response"]["instructions"]
    assert "one brief, polite goodbye" in instruction
    assert "Do not ask a question" in instruction

    await pipeline.close()


@pytest.mark.asyncio
async def test_duplicate_transcription_item_creates_exactly_one_response(
    mock_runtime_config, transport_session
):
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    sent_events = []
    pipeline.send_event = MagicMock(side_effect=sent_events.append)
    message = json.dumps(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-caller-1",
            "transcript": "Yes, I can talk now.",
        }
    )

    await pipeline._handle_dc_message(message)
    await pipeline._handle_dc_message(message)
    await asyncio.sleep(CALLER_TURN_SETTLE_SECS + 0.05)

    assert [event["type"] for event in sent_events] == ["response.create"]
    assert pipeline.policy.turn_epoch == 1

    await pipeline.close()


@pytest.mark.asyncio
async def test_split_caller_audio_produces_one_response_to_latest_meaning(
    mock_runtime_config, transport_session
):
    """Regression for football -> spurious 'Peace' -> 'Thanks for this'."""

    emitted_events = []
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
        event_sink=emitted_events.append,
    )
    pipeline.policy._opening_attempted = True
    pipeline.policy._question_open = True
    sent_events = []
    pipeline.send_event = MagicMock(side_effect=sent_events.append)

    await pipeline._handle_dc_message(
        json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "football"})
    )
    await pipeline._handle_dc_message(
        json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "artifact"})
    )
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "football",
                "transcript": "Most often they are football sports.",
            }
        )
    )
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "artifact",
                "transcript": "Peace.",
            }
        )
    )
    await asyncio.sleep(0.10)
    await pipeline._handle_dc_message(
        json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "thanks"})
    )
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "thanks",
                "transcript": "Thanks for this.",
            }
        )
    )
    await asyncio.sleep(CALLER_TURN_SETTLE_SECS + 0.05)

    response_events = [event for event in sent_events if event["type"] == "response.create"]
    assert len(response_events) == 1
    assert "one brief, polite goodbye" in response_events[0]["response"]["instructions"]
    assert pipeline.policy.turn_epoch == 2
    ignored = [event for event in emitted_events if event.get("turn_admission") == "ignored"]
    assert [event["text"] for event in ignored] == ["Peace."]

    await pipeline.close()


@pytest.mark.asyncio
async def test_barge_in_truncates_unheard_assistant_audio_from_model_context(
    mock_runtime_config, transport_session
):
    flush = AsyncMock(return_value={"status": "ok"})
    transport_session.set_flush_handler(flush)
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    sent_events = []
    pipeline.send_event = MagicMock(side_effect=sent_events.append)

    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "response.created",
                "response": {"id": "resp-truncate", "status": "in_progress"},
            }
        )
    )
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "response.output_item.added",
                "response_id": "resp-truncate",
                "item": {"id": "assistant-item", "type": "message"},
            }
        )
    )
    pipeline._assistant_is_speaking = True
    transport_session.session.metrics.last_rendered_sequence = 24
    await pipeline._handle_dc_message(
        json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "caller-item"})
    )
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "caller-item",
                "transcript": "Wait, I want to ask something else.",
            }
        )
    )

    truncate = next(event for event in sent_events if event["type"] == "conversation.item.truncate")
    assert truncate["item_id"] == "assistant-item"
    assert truncate["content_index"] == 0
    assert truncate["audio_end_ms"] == 500

    await pipeline.close()


@pytest.mark.asyncio
async def test_barge_in_cancels_then_waits_for_done_before_next_response(
    mock_runtime_config, transport_session
):
    flush = AsyncMock(return_value={"status": "ok"})
    transport_session.set_flush_handler(flush)
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    sent_events = []
    pipeline.send_event = MagicMock(side_effect=sent_events.append)

    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item-1",
                "transcript": "Yes, tell me more.",
            }
        )
    )
    await asyncio.sleep(CALLER_TURN_SETTLE_SECS + 0.05)
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "response.created",
                "response": {"id": "resp-1", "status": "in_progress"},
            }
        )
    )
    await pipeline._handle_dc_message(
        json.dumps({"type": "input_audio_buffer.speech_started", "item_id": "item-2"})
    )
    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item-2",
                "transcript": "Actually, what sports do you have?",
            }
        )
    )
    await asyncio.sleep(CALLER_TURN_SETTLE_SECS + 0.05)

    assert [event["type"] for event in sent_events] == [
        "response.create",
        "response.cancel",
        "output_audio_buffer.clear",
    ]
    assert sent_events[-2]["response_id"] == "resp-1"

    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "response.done",
                "response": {"id": "resp-1", "status": "cancelled", "output": []},
            }
        )
    )

    assert [event["type"] for event in sent_events] == [
        "response.create",
        "response.cancel",
        "output_audio_buffer.clear",
    ]
    await pipeline._handle_dc_message(
        json.dumps({"type": "output_audio_buffer.cleared", "response_id": "resp-1"})
    )
    assert [event["type"] for event in sent_events] == [
        "response.create",
        "response.cancel",
        "output_audio_buffer.clear",
        "response.create",
    ]
    assert "sports" in pipeline.policy.last_caller_text.lower()

    await pipeline.close()


@pytest.mark.asyncio
async def test_nonfinal_transcription_event_never_creates_response(
    mock_runtime_config, transport_session
):
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    sent_events = []
    pipeline.send_event = MagicMock(side_effect=sent_events.append)

    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription",
                "item_id": "item-partial",
                "transcript": "partial words",
            }
        )
    )

    assert sent_events == []
    assert pipeline.policy.turn_epoch == 0

    await pipeline.close()


@pytest.mark.asyncio
async def test_attention_check_does_not_repeat_permission_or_callback_question(
    mock_runtime_config, transport_session
):
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
    )
    pipeline.policy._opening_attempted = True
    sent_events = []
    pipeline.send_event = MagicMock(side_effect=sent_events.append)
    pipeline._session_instructions = "Exact persona and task"

    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "HELLO?",
            }
        )
    )
    await asyncio.sleep(CALLER_TURN_SETTLE_SECS + 0.05)

    instruction = sent_events[-1]["response"]["instructions"]
    assert "can hear the caller" in instruction
    assert "Do not repeat" in instruction
    assert "callback question" in instruction

    await pipeline.close()


@pytest.mark.asyncio
async def test_low_confidence_transcript_cannot_advance_sales_state(
    mock_runtime_config, transport_session
):
    emitted_events = []
    pipeline = ChatGPTRealtimePipeline(
        transport=transport_session,
        config=mock_runtime_config,
        auth_manager=MagicMock(),
        caller_id="+33123456789",
        event_sink=emitted_events.append,
    )
    pipeline.policy._opening_attempted = True
    sent_events = []
    pipeline.send_event = MagicMock(side_effect=sent_events.append)

    await pipeline._handle_dc_message(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "uncertain-french",
                "transcript": "Je regarde le football.",
                "languages": [{"code": "fr"}],
                "logprobs": [{"token": "football", "logprob": -3.0}],
            }
        )
    )
    await asyncio.sleep(CALLER_TURN_SETTLE_SECS + 0.05)

    assert "viewing_preferences" not in pipeline.policy.task.state
    assert pipeline.policy._last_caller_intent == "uncertain_audio"
    assert pipeline.policy.reply_language == "fr-FR"
    user_event = next(event for event in emitted_events if event.get("role") == "user")
    assert user_event["transcription_low_confidence"] is True
    assert user_event["detected_language"] == "fr"
    instruction = sent_events[-1]["response"]["instructions"]
    assert "original audio" in instruction
    assert "Do not guess" in instruction

    await pipeline.close()
