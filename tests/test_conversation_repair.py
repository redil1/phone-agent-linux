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
from pipecat.frames.frames import TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection

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


def test_an_already_spoken_sentence_is_blocked(tmp_path: Any) -> None:
    """The real call said the identical sentence twice."""

    runtime = _runtime(tmp_path)
    sentence = "Pour commencer, qu'est-ce que vous regardez le plus souvent ?"
    spoken, stop = runtime.guard_sentence(sentence, is_first=True)
    assert spoken and stop is False

    repeated, stop_again = runtime.guard_sentence(sentence, is_first=True)
    assert repeated == ""
    assert stop_again is True


def test_a_different_sentence_is_still_allowed(tmp_path: Any) -> None:
    """Only repeats are blocked. A second *statement* still passes.

    A second question would now be dropped by the one-question-per-turn rule,
    which is a different guard with its own tests.
    """

    runtime = _runtime(tmp_path)
    runtime.guard_sentence("Vous regardez surtout le sport, d'accord.", is_first=True)
    spoken, stop = runtime.guard_sentence("Je note cela pour la suite.", is_first=False)
    assert spoken
    assert stop is False


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


def test_unclear_turn_is_repaired_and_never_reaches_the_model(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    processor = ConversationRepairProcessor(runtime)
    sink = _Sink(processor)

    _run(processor, "Hello?")

    assert sink.forwarded() == [], "an unclear turn must not reach the model"
    assert len(sink.spoken()) == 1, "the caller must hear a repair instead"


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


def test_repeat_request_repeats_our_own_last_turn(tmp_path: Any) -> None:
    """They did not hear us; moving to new content ignores that."""

    runtime = _runtime(tmp_path)
    runtime.guard_sentence("Vous regardez surtout le sport ?", is_first=True)
    processor = ConversationRepairProcessor(runtime)
    sink = _Sink(processor)

    _run(processor, "Hein ?")

    spoken = sink.spoken()
    assert len(spoken) == 1
    assert "sport" in spoken[0], "the repeat must contain what was actually said"


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
    assert "Could you say it again?" in prompt
    assert "Vous pouvez répéter" not in prompt

    french_prompt = compiler.compile(task_contract=contract, language="fr-FR")
    assert "Vous pouvez répéter" in french_prompt


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

    from phone_agent_gateway.ai_bridge.agent_policy import ResponsePolicyProcessor
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )

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

    from phone_agent_gateway.ai_bridge.agent_policy import ResponsePolicyProcessor
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )

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
