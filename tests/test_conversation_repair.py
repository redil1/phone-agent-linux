"""Tests for the human-conversation guards.

Each case here is a failure that was actually audible on a real call, or the
correct behaviour that replaces it. The wordings all come from the persona
YAML, so these assert behaviour rather than pinning any particular sentence.
"""

from __future__ import annotations

import asyncio
from itertools import pairwise
from typing import Any

import pytest
from pipecat.frames.frames import TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection

from phone_agent_gateway.ai_bridge.agent_policy import AgentPolicyRuntime
from phone_agent_gateway.ai_bridge.conversation_repair import (
    RepairPolicy,
    TurnQuality,
    classify_caller_turn,
    looks_like_noise,
)
from phone_agent_gateway.ai_bridge.human_speech import (
    VariedPhrasePicker,
    detect_language,
    detect_register,
    spoken_numbers,
    violates_language_lock,
)
from phone_agent_gateway.ai_bridge.memory.memory_manager import LayeredMemoryManager
from phone_agent_gateway.ai_bridge.repair_processor import ConversationRepairProcessor

# ----------------------------------------------------------------- classify

@pytest.mark.parametrize(
    ("text", "question_open", "expected"),
    [
        # The three turns that got a full sales pitch on the real call.
        ("Hello?", False, TurnQuality.FRAGMENT),
        ("Mm-hmm.", False, TurnQuality.BACKCHANNEL),
        ("I think.", False, TurnQuality.ACTIONABLE),
        # A real answer must still get through.
        ("Je regarde surtout le sport et les films.", False, TurnQuality.ACTIONABLE),
        # "oui" means an answer only while a question is open.
        ("Oui", True, TurnQuality.ACTIONABLE),
        ("Oui", False, TurnQuality.BACKCHANNEL),
        # One word answers a direct question, but is noise otherwise.
        ("sport", True, TurnQuality.ACTIONABLE),
        ("sport", False, TurnQuality.FRAGMENT),
        # The caller asking us to repeat.
        ("Hein ?", False, TurnQuality.REPEAT_REQUEST),
        ("Pardon", False, TurnQuality.REPEAT_REQUEST),
        ("What?", False, TurnQuality.REPEAT_REQUEST),
        # Wrong moment.
        ("Je conduis là", False, TurnQuality.NOT_NOW),
        ("I'm driving right now", False, TurnQuality.NOT_NOW),
        ("Sorry, I can't talk right now", False, TurnQuality.NOT_NOW),
        ("Please call me back tomorrow", False, TurnQuality.NOT_NOW),
        # Identity challenge.
        ("C'est qui ?", False, TurnQuality.IDENTITY_CHALLENGE),
        ("Who is this?", False, TurnQuality.IDENTITY_CHALLENGE),
        # Noise.
        ("", False, TurnQuality.UNINTELLIGIBLE),
        ("brrr brrr brrr", False, TurnQuality.UNINTELLIGIBLE),
    ],
)
def test_caller_turns_are_classified(text: str, question_open: bool, expected: Any) -> None:
    assert classify_caller_turn(text, question_is_open=question_open) is expected


def test_noise_detection_accepts_real_speech() -> None:
    assert looks_like_noise("brrr brrr brrr") is True
    assert looks_like_noise("   ") is True
    assert looks_like_noise("Je voudrais un abonnement") is False
    assert looks_like_noise("I want the sports package") is False


def test_reported_callback_advice_is_not_the_callers_callback_intent() -> None:
    """Regression for the 2026-09-01 call that poisoned every later turn."""

    coaching = (
        "Sounds like you're opening a sales call. Keep it simple and respectful. "
        "If they hesitate, offer a text call back later."
    )
    assert classify_caller_turn(coaching) is TurnQuality.ACTIONABLE


# ------------------------------------------------------------------- repair

def test_repair_escalates_and_never_repeats_wording() -> None:
    """Hearing the identical apology twice is the machine tell."""

    policy = RepairPolicy(language="fr-FR")
    first, second, third = policy.next_repair(), policy.next_repair(), policy.next_repair()
    assert first and second and third
    assert first != second != third
    assert policy.should_hand_off() is True

    policy.record_success()
    assert policy.consecutive_failures == 0
    assert policy.should_hand_off() is False


def test_repair_wordings_come_from_the_persona_not_code() -> None:
    policy = RepairPolicy(
        language="fr-FR",
        overrides={"first": ["Wording chosen in the Studio."]},
    )
    assert policy.next_repair() == "Wording chosen in the Studio."


def test_varied_picker_avoids_consecutive_repeats() -> None:
    picker = VariedPhrasePicker(pool=("A", "B", "C", "D"))
    picks = [picker.pick() for _ in range(8)]
    assert all(a != b for a, b in pairwise(picks))


# ---------------------------------------------------------------- language

def test_language_lock_blocks_a_wrong_language_reply() -> None:
    """An English reply landed mid-French call despite the prompt forbidding it."""

    assert violates_language_lock("Hello, this is Adam. How can I help you?", "fr-FR") is True
    assert violates_language_lock("Bonjour, je peux vous aider ?", "fr-FR") is False


def test_a_borrowed_word_is_not_a_language_switch() -> None:
    """One shared word must never look decisive."""

    assert detect_language("Vous regardez le sport ou les films ?") != "en"
    assert violates_language_lock("Le sport, d'accord. Et le football ?", "fr-FR") is False
    assert detect_language("ok") == ""


# ----------------------------------------------------------------- numbers

def test_prices_and_small_numbers_are_spoken_as_words() -> None:
    assert "dix euros" in spoken_numbers("C'est 10€ par mois.", "fr-FR")
    assert "ten euros" in spoken_numbers("It is 10 euros a month.", "en-US")
    assert "vingt-cinq" in spoken_numbers("Il y a 25 chaînes.", "fr-FR")
    # A long identifier must stay as digits rather than becoming one huge number.
    assert "0612345678" in spoken_numbers("Votre numéro est 0612345678.", "fr-FR")


# ---------------------------------------------------------------- register

def test_register_is_detected_from_how_the_caller_speaks() -> None:
    assert detect_register("Tu peux m'expliquer ton offre ?") == "tu"
    assert detect_register("Vous pouvez m'expliquer votre offre ?") == "vous"
    assert detect_register("D'accord") == ""


# ------------------------------------------------------- repetition guard

def _runtime(tmp_path: Any) -> AgentPolicyRuntime:
    return AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="iptv_subscription_sales",
        language="fr-FR",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
    )


def test_an_already_spoken_sentence_is_blocked_before_tts(tmp_path: Any) -> None:
    """A retry stays model-owned, but a known duplicate must never be spoken."""

    runtime = _runtime(tmp_path)
    sentence = "Pour commencer, qu'est-ce que vous regardez le plus souvent ?"
    spoken, stop = runtime.guard_sentence(sentence, is_first=True)
    assert spoken and stop is False

    repeated, stop_again = runtime.guard_sentence(sentence, is_first=True)
    assert repeated == ""
    assert stop_again is True
    assert runtime.consume_guard_rejection() == "repeat"


def test_a_mid_turn_repeat_is_dropped_without_a_canned_replacement(tmp_path: Any) -> None:

    runtime = _runtime(tmp_path)
    sentence = "Pour commencer, qu'est-ce que vous regardez le plus souvent ?"
    _spoken, _ = runtime.guard_sentence(sentence, is_first=True)
    repeated, stop = runtime.guard_sentence(sentence, is_first=False)
    assert repeated == ""
    assert stop is True


def test_a_different_sentence_is_still_allowed(tmp_path: Any) -> None:
    """Dialogue policy observes content but does not suppress model output."""

    runtime = _runtime(tmp_path)
    runtime.guard_sentence("Vous regardez surtout le sport, d'accord.", is_first=True)
    spoken, stop = runtime.guard_sentence("Je note cela pour la suite.", is_first=False)
    assert spoken
    assert stop is False


@pytest.mark.asyncio
async def test_nonstreamed_evaluation_still_flags_repetition(
    tmp_path: Any,
) -> None:
    runtime = _runtime(tmp_path)
    sentence = "Would that kind of improvement be worth exploring for you?"

    first, first_evaluation = await runtime.finalize_response(sentence)
    repeated, repeated_evaluation = await runtime.finalize_response(sentence)

    assert first == repeated == sentence
    assert first_evaluation.passed is True
    assert repeated_evaluation.passed is False
    assert "Repeated an earlier AI turn verbatim" in repeated_evaluation.feedback


@pytest.mark.asyncio
async def test_clarification_question_cannot_be_replaced_by_sales_stage_logic(
    tmp_path: Any,
) -> None:
    runtime = _runtime(tmp_path)
    await runtime.finalize_response(
        "Would that kind of improvement be worth exploring for you?"
    )
    await runtime.observe_transcription("What improvements?")

    explanation = (
        "I mean using one service instead of several separate subscriptions, "
        "with the channels you actually watch in one place."
    )
    spoken, evaluation = await runtime.finalize_response(explanation)

    assert spoken == explanation
    assert evaluation.passed is True
    assert runtime._latest_turn_quality == "actionable"
    assert "Respond to the caller's actual meaning directly" in runtime._latest_turn_guidance


@pytest.mark.asyncio
async def test_fragment_does_not_advance_task_or_prospecting_stage(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    await runtime.finalize_response(
        "Hello, this is Adam. Is now a good time for one question?",
        response_kind="greeting",
    )
    await runtime.observe_transcription("Yes.")
    phase_before = runtime.call_context.phase
    task_before = dict(runtime.task.state)

    await runtime.observe_transcription("What's...")

    assert runtime.call_context.phase is phase_before
    assert runtime.task.state == task_before
    assert runtime._latest_turn_quality == "fragment"


def test_question_state_tracks_whether_an_answer_is_expected(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    runtime.guard_sentence("Vous regardez surtout le sport ?", is_first=True)
    assert runtime.classify_turn("Oui") is TurnQuality.ACTIONABLE

    runtime.guard_sentence("Très bien, je note cela pour vous.", is_first=False)
    assert runtime.classify_turn("Oui") is TurnQuality.BACKCHANNEL


# --------------------------------------------------------------- processor

class _Sink:
    def __init__(self, processor: Any) -> None:
        self.frames: list[Any] = []
        processor.push_frame = self._capture  # type: ignore[method-assign]

    async def _capture(self, frame: Any, direction: Any = None) -> None:
        self.frames.append(frame)

    def spoken(self) -> list[str]:
        return [f.text for f in self.frames if isinstance(f, TTSSpeakFrame)]

    def forwarded(self) -> list[str]:
        return [f.text for f in self.frames if isinstance(f, TranscriptionFrame)]


def _run(processor: Any, text: str) -> None:
    asyncio.run(
        processor.process_frame(
            TranscriptionFrame(text=text, user_id="caller", timestamp=None),
            FrameDirection.DOWNSTREAM,
        )
    )


def test_unclear_turn_reaches_the_model_with_repair_guidance(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    processor = ConversationRepairProcessor(runtime)
    sink = _Sink(processor)

    _run(processor, "Hello?")

    assert sink.forwarded() == ["Hello?"]
    assert sink.spoken() == []
    assert runtime._latest_turn_quality == "fragment"
    assert "if it truly remains incomplete, clarify briefly" in runtime._latest_turn_guidance


def test_backchannel_is_ignored_entirely(tmp_path: Any) -> None:
    """Only while the agent is speaking - see the silent-drop regression below.

    "mm-hmm" said over the agent is filler. The same words to a waiting agent
    are the caller's answer, and discarding those left both sides silent.
    """

    import asyncio as _asyncio

    from pipecat.frames.frames import BotStartedSpeakingFrame

    runtime = _runtime(tmp_path)
    processor = ConversationRepairProcessor(runtime)
    sink = _Sink(processor)
    _asyncio.run(processor.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM))

    _run(processor, "Mm-hmm.")

    assert sink.forwarded() == []
    assert sink.spoken() == [], "a person does not answer a backchannel"


def test_real_turn_passes_through_untouched(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    processor = ConversationRepairProcessor(runtime)
    sink = _Sink(processor)

    _run(processor, "Je regarde surtout le sport et les films.")

    assert sink.forwarded() == ["Je regarde surtout le sport et les films."]
    assert sink.spoken() == []


def test_repeat_request_is_delegated_to_the_model_with_context(tmp_path: Any) -> None:

    runtime = _runtime(tmp_path)
    runtime.guard_sentence("Vous regardez surtout le sport ?", is_first=True)
    processor = ConversationRepairProcessor(runtime)
    sink = _Sink(processor)

    _run(processor, "Hein ?")

    assert sink.forwarded() == ["Hein ?"]
    assert sink.spoken() == []
    assert runtime._latest_turn_quality == "repeat_request"
    assert "Rephrase it more simply" in runtime._latest_turn_guidance


def test_repair_can_be_disabled_for_a_controlled_comparison(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    processor = ConversationRepairProcessor(runtime, enabled=False)
    sink = _Sink(processor)

    _run(processor, "Hello?")

    assert sink.forwarded() == ["Hello?"]
    assert sink.spoken() == []


# ------------------------------------------------- regressions from a real call
# Call of 2026-08-26 18:40: the English greeting ended with "Do you have a quick
# minute to chat?", three "Yes" answers were silently dropped, and the one reply
# that did arrive was French ("Parfait, merci.") after 11.5 seconds.

def test_greeting_question_keeps_the_answer_actionable(tmp_path: Any) -> None:
    """Three "Yes" answers to the greeting were discarded as backchannels."""

    import asyncio as _asyncio

    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="iptv_subscription_sales",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
    )
    _asyncio.run(
        runtime.finalize_response(
            "Hello, this is Adam. Do you have a quick minute to chat?",
            response_kind="greeting",
        )
    )
    for answer in ("Yeah.", "Yes.", "Yes, I'm available."):
        assert runtime.classify_turn(answer) is TurnQuality.ACTIONABLE, answer


def test_clear_refusal_outranks_short_backchannel_heuristic(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    runtime.note_opening_attempted()

    assert runtime.classify_turn("No.") is TurnQuality.ACTIONABLE


def test_a_short_wrong_language_reply_is_blocked() -> None:
    """"Parfait, merci." was too short to count markers and slipped through."""

    assert violates_language_lock("Parfait, merci.", "en-US") is True
    assert violates_language_lock("Merci.", "en-US") is True
    assert violates_language_lock("Yes, of course.", "en-US") is False
    assert violates_language_lock("Parfait, merci.", "fr-FR") is False
    # Neutral tokens must still prove nothing either way.
    assert violates_language_lock("ok", "en-US") is False


def _shipped_compiler(tmp_path: Any) -> Any:
    """A compiler using the shipped defaults, not the operator's live persona."""

    import yaml

    from phone_agent_gateway.ai_bridge.personality.persona_compiler import PersonaCompiler

    persona_file = tmp_path / "persona.yaml"
    persona_file.write_text(
        yaml.safe_dump({"identity": {"name": "Adam", "role": "Sales Manager"}}),
        encoding="utf-8",
    )
    return PersonaCompiler(persona_path=persona_file)


def test_english_call_gets_english_repair_wordings(tmp_path: Any) -> None:
    """A French phrasing in an English prompt is what pushed the model to French."""

    compiler = _shipped_compiler(tmp_path)
    english = compiler.repair_phrases("en-US")["first"]
    french = compiler.repair_phrases("fr-FR")["first"]
    assert english and french
    assert english != french
    assert any("Sorry" in phrase or "Excuse" in phrase for phrase in english)
    assert any("Pardon" in phrase or "Excusez" in phrase for phrase in french)


def test_english_prompt_carries_no_french_examples(tmp_path: Any) -> None:
    from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

    compiler = _shipped_compiler(tmp_path)
    contract = TaskEngine().require_contract("iptv_subscription_sales")
    prompt = compiler.compile(task_contract=contract, language="en-US")
    assert "If you did not clearly understand" in prompt
    assert "Could you say it again?" not in prompt
    assert "Vous pouvez répéter" not in prompt

    french_prompt = compiler.compile(task_contract=contract, language="fr-FR")
    assert "Vous pouvez répéter" not in french_prompt


@pytest.mark.asyncio
async def test_long_meta_coaching_cannot_grant_or_refuse_permission(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    runtime.note_opening_attempted()
    coaching = (
        "Keep it simple and say did I catch you at a good time, then if they hesitate "
        "you can say call me back later."
    )

    await runtime.observe_transcription(coaching)

    assert runtime._permission_state == "unknown"
    assert runtime._latest_turn_quality == "actionable"
    assert "permission_to_continue" not in runtime.task.state


def test_permission_accepts_natural_direct_answers_but_not_quoted_examples(
    tmp_path: Any,
) -> None:
    runtime = _runtime(tmp_path)

    assert runtime._classify_permission("Yes, that's fine.") == "granted"
    assert runtime._classify_permission("Sure, I have a minute.") == "granted"
    assert runtime._classify_permission("Sorry, I can't talk right now.") == "refused"
    assert (
        runtime._classify_permission(
            "For example, you can say: is this a good time, then wait."
        )
        == "unknown"
    )


def test_a_flat_override_does_not_erase_the_other_language(tmp_path: Any) -> None:
    """A persona saved before wordings were language-keyed must not poison
    the other language's prompt."""

    import yaml

    from phone_agent_gateway.ai_bridge.personality.persona_compiler import PersonaCompiler

    persona_file = tmp_path / "persona.yaml"
    persona_file.write_text(
        yaml.safe_dump(
            {
                "identity": {"name": "Adam", "role": "Sales Manager"},
                "human_conversation": {
                    "repair": {"ask_again_first": ["Pardon, vous pouvez répéter ?"]}
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    compiler = PersonaCompiler(persona_path=persona_file)
    assert compiler.repair_phrases("fr-FR")["first"] == ["Pardon, vous pouvez répéter ?"]
    english = compiler.repair_phrases("en-US")["first"]
    assert english and all("Pardon" not in phrase for phrase in english)


# ------------------------------------------- regression from the 19:18 call
# The caller said "Yes." then, after 9.4 s of silence, "Yeah." Both turns
# produced a reply and both were spoken, so one question got two answers.

def test_a_reply_superseded_by_a_newer_turn_is_never_spoken(tmp_path: Any) -> None:
    import asyncio as _asyncio

    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )

    from phone_agent_gateway.ai_bridge.agent_policy import ResponsePolicyProcessor

    events: list[dict[str, Any]] = []
    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="iptv_subscription_sales",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
        event_sink=events.append,
    )
    processor = ResponsePolicyProcessor(runtime)
    spoken: list[str] = []

    async def capture(frame: Any, direction: Any = None) -> None:
        if isinstance(frame, LLMTextFrame):
            spoken.append(frame.text)

    processor.push_frame = capture  # type: ignore[method-assign]

    async def scenario() -> None:
        await runtime.observe_transcription("Yes.")
        await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        # The caller gives up waiting and speaks again mid-generation.
        await runtime.observe_transcription("Yeah.")
        await processor.process_frame(
            LLMTextFrame("Great, so what kind of sports do you watch? "),
            FrameDirection.DOWNSTREAM,
        )
        await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    _asyncio.run(scenario())

    assert spoken == [], "a reply to a superseded turn must never be spoken"
    # And no playback status may be attributed to a reply that never played.
    await_started = [e for e in events if e["type"] == "playback_status"]
    assert await_started == []


def test_a_reply_to_the_current_turn_is_still_spoken(tmp_path: Any) -> None:
    import asyncio as _asyncio

    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )

    from phone_agent_gateway.ai_bridge.agent_policy import ResponsePolicyProcessor

    runtime = AgentPolicyRuntime(
        caller_id="anonymous",
        task_id="iptv_subscription_sales",
        language="en-US",
        memory_enabled=False,
        memory_manager=LayeredMemoryManager(storage_path=tmp_path / "memory.json"),
    )
    processor = ResponsePolicyProcessor(runtime)
    spoken: list[str] = []

    async def capture(frame: Any, direction: Any = None) -> None:
        if isinstance(frame, LLMTextFrame):
            spoken.append(frame.text)

    processor.push_frame = capture  # type: ignore[method-assign]

    async def scenario() -> None:
        await runtime.observe_transcription("Yes.")
        await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        await processor.process_frame(
            LLMTextFrame("Great, what do you watch most? "), FrameDirection.DOWNSTREAM
        )
        await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    _asyncio.run(scenario())
    assert spoken == ["Great, what do you watch most?"]


def test_streamed_duplicate_is_regenerated_before_any_tts(tmp_path: Any) -> None:
    import asyncio as _asyncio

    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )

    from phone_agent_gateway.ai_bridge.agent_policy import ResponsePolicyProcessor

    runtime = _runtime(tmp_path)
    repeated = "No problem, I've caught you at a bad time. When would suit you?"
    runtime.guard_sentence(
        "No problem, I've caught you at a bad time.", is_first=True
    )
    runtime.guard_sentence("When would suit you?", is_first=False)
    processor = ResponsePolicyProcessor(runtime)
    spoken: list[str] = []
    retries: list[tuple[str, int]] = []
    resolved: list[int] = []

    async def capture(frame: Any, direction: Any = None) -> None:
        if isinstance(frame, LLMTextFrame):
            spoken.append(frame.text)

    async def retry(text: str, epoch: int) -> bool:
        retries.append((text, epoch))
        return True

    async def resolve(epoch: int) -> None:
        resolved.append(epoch)

    processor.push_frame = capture  # type: ignore[method-assign]
    processor.bind_repetition_recovery(retry, resolve)

    async def scenario() -> None:
        await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        await processor.process_frame(LLMTextFrame(repeated), FrameDirection.DOWNSTREAM)
        await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
        assert spoken == []

        await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        await processor.process_frame(
            LLMTextFrame("You're right; that sounded scripted. What would you like me to change?"),
            FrameDirection.DOWNSTREAM,
        )
        await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    _asyncio.run(scenario())

    assert retries == [
        ("No problem, I've caught you at a bad time.", runtime.turn_epoch)
    ]
    assert spoken == [
        "You're right; that sounded scripted.",
        "What would you like me to change?",
    ]
    assert resolved == [runtime.turn_epoch]


@pytest.mark.asyncio
async def test_pipeline_retry_decontaminates_context_and_targets_latest_turn(
    tmp_path: Any,
) -> None:
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext

    from phone_agent_gateway.ai_bridge.production_pipeline import ProductionCallPipeline

    class _Worker:
        def __init__(self) -> None:
            self.frames: list[Any] = []

        async def queue_frame(self, frame: Any) -> None:
            self.frames.append(frame)

    runtime = _runtime(tmp_path)
    await runtime.observe_transcription(
        "No worries, if now is not good, tell me a rough time window."
    )
    old = "No problem, I've caught you at a bad time. When would suit you?"
    context = LLMContext(
        [
            {"role": "system", "content": "Answer the caller naturally."},
            {"role": "assistant", "content": old},
            {"role": "user", "content": runtime.last_caller_text},
        ]
    )
    pipeline = object.__new__(ProductionCallPipeline)
    pipeline.policy = runtime
    pipeline.context = context
    pipeline.worker = _Worker()
    pipeline._repeat_retry_epoch = -1
    pipeline._repeat_retry_attempts = 0
    pipeline._repeat_retry_message = None

    scheduled = await pipeline._retry_repeated_response(
        "No problem, I've caught you at a bad time.", runtime.turn_epoch
    )

    assert scheduled is True
    assert len(pipeline.worker.frames) == 1
    assert isinstance(pipeline.worker.frames[0], LLMContextFrame)
    assert not any(
        message.get("role") == "assistant" and "caught you at a bad time" in message["content"]
        for message in context.messages
    )
    correction = pipeline._repeat_retry_message
    assert correction is not None
    assert runtime.last_caller_text in correction["content"]

    await pipeline._resolve_repetition_retry(runtime.turn_epoch)
    assert correction not in context.messages


# ------------------------------------------------ regression: silent drops
# Short answers were classified as backchannels whenever the agent's previous
# sentence did not end in "?", then discarded with no log line and nothing in
# the Studio. The caller spoke, saw nothing, and the agent waited forever.

def _bot_frames() -> tuple[Any, Any]:
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame

    return BotStartedSpeakingFrame, BotStoppedSpeakingFrame


def test_a_short_answer_reaches_the_model_when_the_agent_is_silent(tmp_path: Any) -> None:
    """The agent is waiting, so "Yes." is an answer, not filler."""

    runtime = _runtime(tmp_path)
    processor = ConversationRepairProcessor(runtime)
    sink = _Sink(processor)
    assert processor._bot_speaking is False

    _run(processor, "Yes.")

    assert sink.forwarded() == ["Yes."], "a reply to a waiting agent must not be dropped"
    assert sink.spoken() == []


def test_a_backchannel_over_agent_speech_is_still_ignored(tmp_path: Any) -> None:
    """Said *over* the agent, it is filler and must not become a turn."""

    import asyncio as _asyncio

    started, _stopped = _bot_frames()
    runtime = _runtime(tmp_path)
    processor = ConversationRepairProcessor(runtime)
    sink = _Sink(processor)

    _asyncio.run(processor.process_frame(started(), FrameDirection.DOWNSTREAM))
    assert processor._bot_speaking is True

    _run(processor, "Mm-hmm.")
    assert sink.forwarded() == []
    assert sink.spoken() == []


def test_backchannel_handling_follows_the_agent_speaking_state(tmp_path: Any) -> None:
    import asyncio as _asyncio

    started, stopped = _bot_frames()
    runtime = _runtime(tmp_path)
    processor = ConversationRepairProcessor(runtime)
    sink = _Sink(processor)

    _asyncio.run(processor.process_frame(started(), FrameDirection.DOWNSTREAM))
    _run(processor, "Yeah.")
    assert sink.forwarded() == [], "ignored while the agent is talking"

    _asyncio.run(processor.process_frame(stopped(), FrameDirection.DOWNSTREAM))
    _run(processor, "Yeah.")
    assert sink.forwarded() == ["Yeah."], "accepted once the agent has finished"


def test_arabic_script_is_named_not_mistaken_for_french() -> None:
    """Darija typed in Arabic was labelled French, so the agent replied in French."""

    from phone_agent_gateway.ai_bridge.human_speech import (
        detect_language,
        detect_script_language,
    )

    darija = "والله ما فهمتش، واش تقولي لي دابا؟"
    assert detect_script_language(darija) == "ar"
    assert detect_language(darija) == "ar"
    assert detect_language(darija) not in ("fr", "en")


def test_other_non_latin_scripts_are_named_too() -> None:
    from phone_agent_gateway.ai_bridge.human_speech import detect_language

    assert detect_language("Привет как дела друг") == "ru"
    assert detect_language("こんにちは、元気ですか") == "ja"


def test_latin_detection_is_unchanged() -> None:
    from phone_agent_gateway.ai_bridge.human_speech import detect_language

    assert detect_language("Bien sûr, merci beaucoup") == "fr"
    assert detect_language("Yes, go ahead please") == "en"
    # A stray foreign-sounding fragment stays undecidable rather than guessing.
    assert detect_language("Ja, ja noch.") == ""
