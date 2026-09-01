"""Tests for the provider-independent personality and task policy."""

from __future__ import annotations

from typing import Any

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
)
from pipecat.processors.aggregators.llm_response_universal import LLMContext
from pipecat.processors.frame_processor import FrameDirection

from phone_agent_gateway.ai_bridge.agent_policy import (
    AgentPolicyRuntime,
    PlaybackEventProcessor,
    ResponsePolicyProcessor,
)
from phone_agent_gateway.ai_bridge.memory.memory_manager import LayeredMemoryManager
from phone_agent_gateway.ai_bridge.session import CallSessionState


@pytest.mark.asyncio
async def test_policy_compiles_context_evaluates_and_remembers(tmp_path: Any) -> None:
    events: list[dict[str, Any]] = []
    memory = LayeredMemoryManager(storage_path=tmp_path / "memory.json")
    runtime = AgentPolicyRuntime(
        caller_id="+212 600 000 000",
        task_id="customer_support",
        language="en-US",
        additional_instructions="Explain why the call exists.",
        memory_manager=memory,
        event_sink=events.append,
    )

    assert "ACTIVE TASK CONTRACT (customer_support)" in runtime.system_prompt
    assert "Connected Tools: none" in runtime.system_prompt
    assert "Explain why the call exists" in runtime.system_prompt

    await runtime.observe_transcription("My name is Omar and I prefer English")
    spoken, evaluation = await runtime.finalize_response(
        "I booked the appointment and sent the confirmation."
    )
    assert "cannot confirm" in spoken
    assert evaluation.passed is False
    await runtime.close()

    saved = memory.get_caller_memory("+212600000000")
    assert saved["name"] == "Omar"
    assert saved["preferences"]["preferred_language"] == "en-US"
    assert saved["call_count"] == 1
    assert saved["episodic_turns"][0]["task_id"] == "customer_support"
    # The conversation events still arrive in order; task-state and outcome
    # events are additional and are asserted separately below.
    conversation = [
        event["type"]
        for event in events
        if event["type"] in {"transcript", "evaluation", "playback_status"}
    ]
    assert conversation == ["transcript", "transcript", "evaluation", "playback_status"]
    playback = [event for event in events if event["type"] == "playback_status"]
    assert playback[-1]["status"] == "interrupted"

    # Every call now ends with a recorded disposition rather than nothing.
    outcomes = [event for event in events if event["type"] == "call_outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["task_id"] == "customer_support"
    assert outcomes[0]["outcome"]
    evaluations = [event for event in events if event["type"] == "evaluation"]
    assert evaluations[-1]["task_score"] == 100.0


@pytest.mark.asyncio
async def test_cold_outbound_yes_builds_relevance_before_product_slots(tmp_path: Any) -> None:
    events: list[dict[str, Any]] = []
    runtime = AgentPolicyRuntime(
        caller_id="+212600000000",
        task_id="iptv_subscription_sales",
        language="en-US",
        call_direction="outbound",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
        event_sink=events.append,
    )
    await runtime.finalize_response(
        "Hello, this is Adam. Is now a good time for a quick chat?",
        response_kind="greeting",
    )

    await runtime.observe_transcription("Yes.")

    state = runtime.live_state_instructions()
    assert "call_direction: outbound" in state
    assert "conversation_mode: cold_prospecting" in state
    assert "prospect_interest: unknown" in state
    assert "explicit_product_interest_observed: no" in state
    assert "uncollected_context (discover only when natural)" in state
    assert "Ask one open, non-product question" in state
    assert "This is advisory context, not a script" in state
    assert any(
        event.get("type") == "call_context"
        and event.get("phase") == "relevance_discovery"
        for event in events
    )
    model_reply = "Great. What device do you plan to watch on first, like a Smart TV or Firestick?"
    spoken, stop = runtime.guard_sentence(model_reply, is_first=True)
    assert spoken == model_reply
    assert stop is False


def test_inbound_intent_does_not_block_relevant_product_qualification() -> None:
    runtime = AgentPolicyRuntime(
        caller_id="+212600000000",
        task_id="iptv_subscription_sales",
        language="en-US",
        call_direction="inbound",
        memory_enabled=False,
    )

    sentence = "Which device would you like help setting up?"
    spoken, stop = runtime.guard_sentence(sentence, is_first=True)

    assert spoken == sentence
    assert stop is False
    runtime.note_opening_attempted()
    assert "permission_to_continue: granted" in runtime.live_state_instructions()
    assert "current_conversation_stage: INTENT_DISCOVERY" in runtime.live_state_instructions()


def test_realtime_prompt_distinguishes_outbound_and_inbound_calls() -> None:
    compiler = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="iptv_subscription_sales",
        language="en-US",
        call_direction="inbound",
        memory_enabled=False,
    ).persona_compiler

    outbound = compiler.compile_realtime(
        task_contract={"id": "test", "objective": "Help with television."},
        call_direction="outbound",
    )
    inbound = compiler.compile_realtime(
        task_contract={"id": "test", "objective": "Help with television."},
        call_direction="inbound",
    )

    assert "OUTBOUND COLD PROSPECTING" in outbound
    assert "Permission to continue" in outbound
    assert "usually establish relevance before product qualification" in outbound
    assert "latest meaning always outranks the suggested sales phase" in outbound
    assert "INBOUND INTENT-LED" in inbound
    assert "caller initiated this call" in inbound.lower()
    assert "cold-sales permission script" in inbound


def test_policy_rejects_unknown_task() -> None:
    with pytest.raises(ValueError, match="unknown task contract"):
        AgentPolicyRuntime(
            caller_id="anonymous",
            task_id="not-a-task",
            language="en-US",
            memory_enabled=False,
        )


@pytest.mark.asyncio
async def test_playback_events_distinguish_completed_and_interrupted_audio(tmp_path: Any) -> None:
    events: list[dict[str, Any]] = []
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="customer_support",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
        event_sink=events.append,
    )

    await runtime.finalize_response("First response")
    await runtime.playback_started()
    await runtime.playback_stopped()
    await runtime.finalize_response("Second response")
    await runtime.playback_started()
    await runtime.mark_playback_interrupted()
    await runtime.playback_stopped()

    statuses = [event["status"] for event in events if event["type"] == "playback_status"]
    assert statuses == ["playing", "completed", "playing", "interrupted"]
    transcript_events = [event for event in events if event["type"] == "transcript"]
    assert transcript_events[0]["response_id"] == "response-1"
    assert transcript_events[0]["delivery_status"] == "generated"


@pytest.mark.asyncio
async def test_upstream_tts_error_marks_pending_audio_failed(tmp_path: Any) -> None:
    events: list[dict[str, Any]] = []
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="customer_support",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
        event_sink=events.append,
    )
    await runtime.finalize_response("This response should be spoken")
    processor = ResponsePolicyProcessor(runtime)

    async def discard(_frame: Any, _direction: FrameDirection) -> None:
        return None

    processor.push_frame = discard  # type: ignore[method-assign]
    await processor.process_frame(
        ErrorFrame(error="TTS first-audio deadline exceeded"),
        FrameDirection.UPSTREAM,
    )

    failures = [event for event in events if event["type"] == "playback_status"]
    assert failures == [
        {
            "type": "playback_status",
            "response_id": "response-1",
            "status": "failed",
            "message": "TTS first-audio deadline exceeded",
        }
    ]


@pytest.mark.asyncio
async def test_sales_call_state_survives_interruption_without_rewriting_model_dialogue(
    tmp_path: Any,
) -> None:
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="iptv_subscription_sales",
        language="fr-FR",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
    )
    context = LLMContext()
    context.add_message({"role": "system", "content": runtime.system_prompt})
    runtime.attach_context(context)

    greeting, _evaluation = await runtime.finalize_response(
        "Bonjour, ici Adam de chez OXzoon. Est-ce un bon moment pour échanger ?",
        response_kind="greeting",
    )
    assert greeting.startswith("Bonjour")
    await runtime.observe_transcription("Oui, vas-y.")

    state = context.get_messages()[-1]["content"]
    assert "opening_already_attempted: yes" in state
    assert "permission_to_continue: granted" in state
    assert "current_conversation_stage: DISCOVER" in state

    repeated, _evaluation = await runtime.finalize_response(
        "Bonjour, je suis Adam de chez OXzoon. Je vous appelle pour vous présenter nos "
        "abonnements IPTV. Est-ce que vous avez quelques minutes pour en discuter ?"
    )

    assert repeated.startswith("Bonjour, je suis Adam")
    assert "je vous appelle" in repeated.casefold()
    await runtime.playback_started()
    await runtime.mark_playback_interrupted()
    state = context.get_messages()[-1]["content"]
    assert "last_ai_turn_delivery: interrupted" in state
    assert "Never repeat the last AI sentence" in state


@pytest.mark.asyncio
async def test_repeated_english_permission_request_is_evaluated_not_rewritten(
    tmp_path: Any,
) -> None:
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="iptv_subscription_sales",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
    )
    await runtime.finalize_response(
        "Hello, this is Adam at OXzoon. Is this a good time for a quick conversation?",
        response_kind="greeting",
    )
    await runtime.observe_transcription("Yes, please go ahead.")
    repeated, _evaluation = await runtime.finalize_response(
        "Hello, this is Adam from OXzoon. I'm calling about IPTV subscriptions. "
        "Is this a good time for a quick conversation?"
    )

    assert repeated.startswith("Hello, this is Adam")


@pytest.mark.asyncio
async def test_goodbye_closes_live_state_without_another_sales_question(tmp_path: Any) -> None:
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="iptv_subscription_sales",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
    )
    await runtime.finalize_response(
        "Hello, this is Adam from OXzoon. Is this a good time to talk?",
        response_kind="greeting",
    )
    await runtime.observe_transcription("Bye.")

    live_state = runtime.live_state_instructions()
    assert "latest_caller_intent: goodbye" in live_state
    assert "current_conversation_stage: CLOSE" in live_state
    assert "close briefly with no sales question" in live_state


@pytest.mark.asyncio
async def test_attention_check_with_natural_modifier_never_grants_permission(
    tmp_path: Any,
) -> None:
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="iptv_subscription_sales",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
    )
    await runtime.finalize_response(
        "Hello, this is Adam from OXzoon. Is this a good time to talk?",
        response_kind="greeting",
    )

    await runtime.observe_transcription("Can you hear me okay?")

    assert runtime._last_caller_intent == "attention_check"
    assert runtime._permission_state == "unknown"
    assert "permission_to_continue" not in runtime.task.state


@pytest.mark.asyncio
async def test_caller_language_switch_updates_the_reply_language(tmp_path: Any) -> None:
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="customer_support",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
    )

    await runtime.observe_transcription(
        "Est-ce que vous m'entendez bien maintenant ?", language_code="fr"
    )

    assert runtime.reply_language == "fr-FR"
    assert runtime._last_caller_intent == "attention_check"
    assert "reply_language: French" in runtime.live_state_instructions()


@pytest.mark.asyncio
async def test_playback_reports_not_delivered_when_no_audio_reached_the_phone(
    tmp_path: Any,
) -> None:
    """A dead uplink must never be reported to the operator as played."""

    events: list[dict[str, Any]] = []
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="customer_support",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
        event_sink=events.append,
    )

    await runtime.finalize_response("This response never reaches the modem")
    await runtime.playback_started()
    await runtime.playback_stopped(delivered_frames=0, dropped_frames=42)

    statuses = [event["status"] for event in events if event["type"] == "playback_status"]
    assert statuses == ["playing", "not_delivered"]
    final = [event for event in events if event["type"] == "playback_status"][-1]
    assert "42" in final["message"]


@pytest.mark.asyncio
async def test_zero_frame_barge_in_is_reported_as_interrupted_not_audio_failure(
    tmp_path: Any,
) -> None:
    events: list[dict[str, Any]] = []
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="customer_support",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
        event_sink=events.append,
    )

    await runtime.finalize_response("The caller interrupts immediately")
    await runtime.playback_started()
    await runtime.mark_playback_interrupted()
    await runtime.playback_stopped(delivered_frames=0)

    statuses = [event["status"] for event in events if event["type"] == "playback_status"]
    assert statuses == ["playing", "interrupted"]


@pytest.mark.asyncio
async def test_playback_reports_completed_when_frames_reached_the_phone(
    tmp_path: Any,
) -> None:
    events: list[dict[str, Any]] = []
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="customer_support",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
        event_sink=events.append,
    )

    await runtime.finalize_response("This response is really spoken")
    await runtime.playback_started()
    await runtime.playback_stopped(delivered_frames=75, dropped_frames=0)

    statuses = [event["status"] for event in events if event["type"] == "playback_status"]
    assert statuses == ["playing", "completed"]


@pytest.mark.asyncio
async def test_playback_processor_derives_delivery_from_session_counters(
    tmp_path: Any,
) -> None:
    """The processor must judge delivery from transport counters, not TTS frames."""

    events: list[dict[str, Any]] = []
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="customer_support",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
        event_sink=events.append,
    )
    await runtime.finalize_response("Spoken into a dead uplink")

    session = CallSessionState()
    processor = PlaybackEventProcessor(runtime, session)

    async def discard(_frame: Any, _direction: FrameDirection) -> None:
        return None

    processor.push_frame = discard  # type: ignore[method-assign]

    await processor.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    # Pipecat emitted bot-speaking, but every transport write failed.
    session.metrics.dropped_output_frames += 30
    await processor.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    statuses = [event["status"] for event in events if event["type"] == "playback_status"]
    assert statuses == ["playing", "not_delivered"]


class _Collect:
    """Capture what the policy processor releases downstream."""

    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def __call__(self, frame: Any, direction: Any = None) -> None:
        self.frames.append(frame)

    def spoken(self) -> list[str]:
        from pipecat.frames.frames import LLMTextFrame

        return [f.text for f in self.frames if isinstance(f, LLMTextFrame)]


async def _runtime(tmp_path: Any, events: list[dict[str, Any]]) -> AgentPolicyRuntime:
    return AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="customer_support",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
        event_sink=events.append,
    )


async def _run_response(processor: Any, chunks: list[str]) -> None:
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )

    await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    for chunk in chunks:
        await processor.process_frame(LLMTextFrame(chunk), FrameDirection.DOWNSTREAM)
    await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_sentences_are_spoken_before_the_model_finishes(tmp_path: Any) -> None:
    """The first sentence must reach TTS while later tokens are still arriving."""

    events: list[dict[str, Any]] = []
    runtime = await _runtime(tmp_path, events)
    processor = ResponsePolicyProcessor(runtime)
    collect = _Collect()
    processor.push_frame = collect  # type: ignore[method-assign]

    from pipecat.frames.frames import LLMFullResponseStartFrame, LLMTextFrame

    await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(LLMTextFrame("We have three plans. "), FrameDirection.DOWNSTREAM)
    # Released already, without waiting for the response to end.
    assert collect.spoken() == ["We have three plans."]

    await processor.process_frame(LLMTextFrame("Which suits you?"), FrameDirection.DOWNSTREAM)
    from pipecat.frames.frames import LLMFullResponseEndFrame

    await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    assert collect.spoken() == ["We have three plans.", "Which suits you?"]

    transcripts = [e for e in events if e["type"] == "transcript" and e["role"] == "assistant"]
    assert len(transcripts) == 1, "a streamed turn must still be recorded once"
    assert transcripts[0]["text"] == "We have three plans. Which suits you?"


@pytest.mark.asyncio
async def test_unverified_action_claim_is_never_spoken(tmp_path: Any) -> None:
    """A guard substitution must also stop the rest of the turn."""

    events: list[dict[str, Any]] = []
    runtime = await _runtime(tmp_path, events)
    processor = ResponsePolicyProcessor(runtime)
    collect = _Collect()
    processor.push_frame = collect  # type: ignore[method-assign]

    await _run_response(
        processor,
        ["I booked your appointment. ", "You will get an email shortly."],
    )
    spoken = " ".join(collect.spoken())
    assert "booked" not in spoken.lower()
    assert "cannot confirm" in spoken.lower()
    # The model's continuation must not follow wording the caller never heard.
    assert "email" not in spoken.lower()


@pytest.mark.asyncio
async def test_run_on_reply_still_reaches_speech(tmp_path: Any) -> None:
    """A model that never punctuates must not block audio forever."""

    events: list[dict[str, Any]] = []
    runtime = await _runtime(tmp_path, events)
    processor = ResponsePolicyProcessor(runtime)
    collect = _Collect()
    processor.push_frame = collect  # type: ignore[method-assign]

    from pipecat.frames.frames import LLMFullResponseStartFrame, LLMTextFrame

    await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(LLMTextFrame("word " * 60), FrameDirection.DOWNSTREAM)
    assert collect.spoken(), "run-on text must be released without terminal punctuation"


@pytest.mark.asyncio
async def test_empty_response_releases_its_reserved_playback_id(tmp_path: Any) -> None:
    events: list[dict[str, Any]] = []
    runtime = await _runtime(tmp_path, events)
    processor = ResponsePolicyProcessor(runtime)
    collect = _Collect()
    processor.push_frame = collect  # type: ignore[method-assign]

    await _run_response(processor, ["   "])
    assert collect.spoken() == []
    # Nothing was spoken, so no playback status may later be attributed to it.
    await runtime.playback_started()
    assert [e for e in events if e["type"] == "playback_status"] == []
