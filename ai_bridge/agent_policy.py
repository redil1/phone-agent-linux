"""Small provider-independent policy layer for every PhoneAgent call."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .call_context import CallContextPolicy
from .conversation_repair import (
    RepairPolicy,
    TurnQuality,
    caller_authorizes_repetition,
    classify_caller_turn,
)
from .guardrails.permission_gate import PermissionGate
from .guardrails.personality_judge import PersonalityFidelityJudge, TurnEvaluationResult
from .human_speech import (
    VariedPhrasePicker,
    acknowledgements_for,
    detect_language,
    detect_register,
    normalize_for_speech,
)
from .memory.memory_manager import LayeredMemoryManager
from .memory.memory_writer import ValidatedMemoryWriter
from .personality.persona_compiler import PersonaCompiler
from .tasks.call_state import CallOutcome, TaskRuntime
from .tasks.task_engine import TaskEngine
from .turn_continuity import looks_semantically_incomplete

EventSink = Callable[[dict[str, Any]], Any]
logger = logging.getLogger("PhoneAgentPolicy")


class AgentPolicyRuntime:
    """One call's persona, task, caller memory, evaluation, and persistence."""

    def __init__(
        self,
        *,
        caller_id: str,
        task_id: str,
        language: str,
        call_direction: str = "outbound",
        additional_instructions: str = "",
        memory_enabled: bool = True,
        available_tools: set[str] | None = None,
        event_sink: EventSink | None = None,
        memory_manager: LayeredMemoryManager | None = None,
    ) -> None:
        self.caller_id = LayeredMemoryManager.normalize_caller_id(caller_id)
        self.task_id = task_id
        self.language = language
        self.call_context = CallContextPolicy(call_direction)
        self.available_tools = available_tools or set()
        self.event_sink = event_sink
        self.memory_enabled = memory_enabled and not self.caller_id.startswith("unknown:")
        self.memory_manager = memory_manager or LayeredMemoryManager()
        self.memory_writer = ValidatedMemoryWriter(self.memory_manager)
        self.persona_compiler = PersonaCompiler()
        identity_status = self.persona_compiler.identity_kernel.production_status()
        if not identity_status["ready"]:
            raise ValueError(
                "Active Identity Kernel profile is not production-ready: "
                f"{identity_status['evaluator_version']} score={identity_status['score']}"
            )
        self.task_engine = TaskEngine()
        self.task_contract = self.task_engine.require_contract(task_id)
        # Slots, stage and outcome are tracked in code. The contract listed what
        # to discover and nothing checked it, so the agent re-asked answered
        # questions and never knew when the task was done.
        self.task = TaskRuntime(self.task_contract)
        if any(slot.id == "preferred_language" for slot in self.task.slots):
            preferred = "French" if language.lower().startswith("fr") else "English"
            self.task.record("preferred_language", preferred)
        self.caller_memory = (
            self.memory_manager.get_caller_memory(self.caller_id) if self.memory_enabled else None
        )
        self.system_prompt = self.persona_compiler.compile(
            caller_memory=self.caller_memory,
            task_contract=self.task_contract,
            language=language,
            call_direction=self.call_context.direction.value,
            additional_instructions=additional_instructions,
            available_tools=self.available_tools,
            caller_id=self.caller_id,
        )
        self._persona_compile_args = {
            "language": language,
            "call_direction": self.call_context.direction.value,
            "additional_instructions": additional_instructions,
        }
        self.judge = PersonalityFidelityJudge()
        # The persona owns every wording; this guard only decides *when* one is
        # needed, which is the part the model cannot judge from text alone.
        self.repair = RepairPolicy(
            language=language,
            overrides=self.persona_compiler.repair_phrases(language),
        )
        self.acknowledgements = VariedPhrasePicker(pool=acknowledgements_for(language))
        # These are observational. They inform the live prompt and evaluator;
        # they never substitute canned dialogue into the model's response.
        self._spoken_sentences: deque[str] = deque(maxlen=24)
        self._completed_ai_turns: deque[str] = deque(maxlen=12)
        self._question_open = False
        # Incremented on every caller turn. A reply generated for an older turn
        # is stale: the caller has already moved on, and speaking it produces
        # two answers to two versions of the same question.
        self._turn_epoch = 0
        self._caller_register = ""
        self._caller_language = "fr" if language.lower().startswith("fr") else "en"
        self.last_caller_text = ""
        self.last_caller_transcript_trusted = True
        self.last_caller_transcription_confidence: float | None = None
        self.recent_caller_turns: deque[tuple[str, bool]] = deque(maxlen=6)
        self._turn_started_at = 0.0
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._response_sequence = 0
        self._pending_playback_ids: deque[str] = deque()
        self._active_playback_id: str | None = None
        self._playback_interrupted = False
        self._live_context: Any | None = None
        self._live_state_message: dict[str, str] | None = None
        self._opening_attempted = False
        self._permission_state = "unknown"
        self._conversation_stage = "OPEN"
        self._last_caller_intent = "unknown"
        self._latest_turn_quality = "unknown"
        self._latest_turn_guidance = (
            "Listen to the caller's latest meaning and answer it directly."
        )
        self._last_ai_response = ""
        self._last_ai_delivery = "none"
        self._last_guard_rejection = ""
        self._repeat_authorized_epoch = -1
        self._closed = False

    def recompile_system_prompt(self) -> str:
        """Rebuild the persona now that the live tool set is known.

        The prompt is compiled in __init__, before any tool backend has been
        reached, so it would otherwise tell the model "Connected Tools: none"
        while the runtime was holding twenty of them — and a persona instructed
        it had no tools does not call any.
        """

        self.system_prompt = self.persona_compiler.compile(
            caller_memory=self.caller_memory,
            task_contract=self.task_contract,
            available_tools=self.available_tools,
            caller_id=self.caller_id,
            **self._persona_compile_args,
        )
        return self.system_prompt

    def attach_context(self, context: Any) -> None:
        """Attach one mutable, provider-independent live-state system message."""

        self._live_context = context
        self._live_state_message = {"role": "system", "content": ""}
        context.add_message(self._live_state_message)
        self._refresh_live_state()

    @staticmethod
    def _normalized_intent_text(text: str) -> str:
        normalized = re.sub(r"[^\wÀ-ÿ'\u2019]+", " ", text.casefold())
        return " ".join(normalized.replace("'", " ").replace("\u2019", " ").split())

    def _classify_permission(self, text: str) -> str:
        normalized = self._normalized_intent_text(text)
        if not normalized:
            return "unknown"
        # Permission is a high-impact state transition, so only accept a short
        # direct answer. Substring searches used to grant/refuse permission
        # from quoted examples such as "say: did I catch you at a good time?".
        if len(normalized.split()) > 18:
            return "unknown"
        positive = (
            r"(?:oui|yes|yeah|yep|okay|ok|d accord|bien sur|certainement)",
            r"(?:oui|yes|okay|ok)?\s*(?:vas y|allez y|go ahead|please continue|"
            r"continuez|je vous ecoute)",
            r"(?:yes|yeah|oui)\s+(?:i am|i m|je suis)\s+(?:available|free|disponible)",
            r"(?:yes|yeah|sure|okay|ok|oui)\s+(?:(?:that|it) s?\s+)?"
            r"(?:fine|okay|ok|good|bon|bien)",
            r"(?:yes|yeah|sure|oui)\s+(?:you can|i have (?:a )?(?:minute|moment)|"
            r"i can talk|vous pouvez)",
            r"(?:yes|oui)?\s*(?:this is|it is|c est)?\s*(?:a )?(?:good time|bon moment)",
            r"(?:tell me more|sounds interesting|i am interested|je suis interesse|"
            r"je suis intéressé|dites m en plus|ca m interesse|ça m intéresse)",
        )
        negative = (
            r"(?:no|non|no thank you|non merci|stop|not interested|pas interesse|pas intéressé)",
            r"(?:no thanks|non merci)?\s*(?:i am|i m|je suis)?\s*"
            r"(?:not interested|pas interesse|pas intéressé)",
            r"(?:no|non)\s+(?:not interested|pas interesse|pas intéressé)",
            r"(?:please )?(?:don t call|do not call|stop calling|ne m appelez plus)"
            r"(?: me)?(?: again)?",
            r"(?:sorry )?(?:this is|it is|c est)?\s*(?:a )?(?:bad time|mauvais moment)",
            r"(?:no |sorry )?(?:i can t|i cannot)\s+talk(?: right now| now)?",
            r"(?:no |sorry )?(?:now|this)\s+is\s+not\s+(?:a )?good time",
        )
        if any(re.fullmatch(pattern, normalized) for pattern in negative):
            return "refused"
        if any(re.fullmatch(pattern, normalized) for pattern in positive):
            return "granted"
        return "unknown"

    def _is_goodbye(self, text: str) -> bool:
        normalized = self._normalized_intent_text(text)
        return bool(
            re.search(
                r"^(?:bye|goodbye|see you|talk later|au revoir|salut|a bientot|à bientôt|"
                r"bonne journee|bonne journée|je dois y aller|i have to go|"
                r"thanks for (?:this|the information|your time)|"
                r"thank you that(?:'s| is) all|merci pour (?:tout|ces informations))"
                r"[.! ]*$",
                normalized,
            )
        )

    def _is_attention_check(self, text: str) -> bool:
        normalized = self._normalized_intent_text(text)
        return any(
            re.search(pattern, normalized)
            for pattern in (
                r"^(?:hello|hello there|hi|hey|allo|all[oô])"
                r"(?: (?:are you there|can you hear me|vous m entendez|tu m entends))?"
                r"(?: (?:okay|ok|clearly|bien|maintenant|toujours|please|s il vous plait))?$",
                r"^(?:are you (?:still )?there|you (?:still )?there|"
                r"can you hear me(?:(?: okay| ok| clearly| now)){0,2}|"
                r"do you hear me(?:(?: okay| ok| clearly| now)){0,2}|"
                r"vous m entendez(?:(?: bien| clairement| maintenant| toujours)){0,2}|"
                r"est ce que vous m entendez(?:(?: bien| clairement| maintenant)){0,2}|"
                r"tu m entends(?:(?: bien| clairement| maintenant| toujours)){0,2})"
                r"(?: please| s il vous plait)?$",
            )
        )

    @property
    def reply_language(self) -> str:
        """Locale used for deterministic wording on the caller's current turn."""

        return "fr-FR" if self._caller_language == "fr" else "en-US"

    async def _adopt_caller_language(self, language_code: str | None) -> None:
        code = (language_code or "").strip().lower().replace("_", "-").split("-", 1)[0]
        if code not in {"en", "fr"} or code == self._caller_language:
            return
        self._caller_language = code
        self.repair.language = self.reply_language
        self.repair.overrides = self.persona_compiler.repair_phrases(self.reply_language)
        self.acknowledgements = VariedPhrasePicker(pool=acknowledgements_for(self.reply_language))
        if any(slot.id == "preferred_language" for slot in self.task.slots):
            changed = self.task.record(
                "preferred_language", "French" if code == "fr" else "English"
            )
            if changed.changed:
                await self._emit({"type": "task_state", **self.task.summary()})
        logger.info("Caller language switched to %s", code)

    def live_state_instructions(self) -> str:
        """Return advisory state for the model without scripting its next reply."""

        last_response = " ".join(self._last_ai_response.split())[:300] or "none"
        known = "; ".join(f"{key}={value}" for key, value in self.task.state.items()) or "none"
        missing = self.task.missing_slots()
        missing_names = ", ".join(slot.id for slot in missing) or "none"
        next_question = "none"
        if missing:
            nxt = missing[0]
            next_question = nxt.question or f"what their {nxt.id.replace('_', ' ')} is"
        strategy_hint, _permitted_question = self.call_context.steering(next_question)
        return (
            "# INTERNAL LIVE CALL CONTEXT — never read or mention this block aloud\n"
            "This is advisory context, not a script. The caller's latest meaning always outranks "
            "task order, sales stages, missing fields, and sample phrases.\n"
            f"{self.call_context.state_block(next_question)}\n"
            f"opening_already_attempted: {'yes' if self._opening_attempted else 'no'}\n"
            f"permission_to_continue: {self._permission_state}\n"
            f"current_conversation_stage: {self._conversation_stage}\n"
            f"latest_caller_intent: {self._last_caller_intent}\n"
            f"latest_caller_turn_quality_hint: {self._latest_turn_quality}\n"
            f"latest_turn_guidance_hint: {self._latest_turn_guidance}\n"
            f"reply_language: {'French' if self._caller_language == 'fr' else 'English'}\n"
            f"last_ai_turn_delivery: {self._last_ai_delivery}\n"
            f"last_ai_turn_text: {last_response}\n"
            f"facts_already_collected: {known}\n"
            f"uncollected_context (discover only when natural): {missing_names}\n"
            f"optional_next_topic: {next_question}\n"
            f"strategy_hint (optional): {strategy_hint}\n"
            "Quality and stage fields are fallible hints derived from audio and simple state; "
            "never let a hint contradict or replace the caller's actual words. "
            "Natural conversation rules: Answer direct questions, corrections, confusion, and "
            "requests to explain before pursuing the objective. If the latest words may be "
            "garbled or incomplete, do not guess and do not advance task state; ask one brief, "
            "context-aware clarification. Never use a generic fallback question. Never repeat "
            "the last AI sentence; rephrase or respond to what changed. Missing task fields are "
            "notes for later, never reasons to ignore the caller. If latest_caller_intent is "
            "goodbye or permission_refused, close briefly with no sales question."
        )

    def _refresh_live_state(self) -> None:
        if self._live_state_message is None:
            return
        self._live_state_message["content"] = self.live_state_instructions()
        # Keep changing state immediately before the newest caller turn instead
        # of permanently at message index 1. Ollama can now reuse the stable
        # persona and dialogue prefix; only this small state block and the new
        # turn require prefill as a call grows.
        messages = getattr(self._live_context, "messages", None)
        if isinstance(messages, list):
            with contextlib.suppress(ValueError):
                messages.remove(self._live_state_message)
            messages.append(self._live_state_message)

    def classify_turn(self, text: str) -> TurnQuality:
        """Decide whether this turn carries something to answer at all."""

        if looks_semantically_incomplete(text):
            return TurnQuality.FRAGMENT
        # Terminal intent and an answer to the still-pending opening permission
        # question are meaningful even when they are only one word. Do not apply
        # this to ordinary "yes"/"okay" backchannels later in the conversation.
        permission = self._classify_permission(text)
        awaiting_opening_answer = (
            self._opening_attempted
            and self._permission_state == "unknown"
            and permission != "unknown"
        )
        if self._is_goodbye(text) or awaiting_opening_answer:
            return TurnQuality.ACTIONABLE
        return classify_caller_turn(
            text,
            question_is_open=self._question_open,
            language=self.reply_language,
        )

    def matches_expected_answer(self, text: str) -> bool:
        """Whether a short utterance fills a still-open task slot."""

        return any(slot.match(text) for slot in self.task.missing_slots())

    def is_explicit_conversation_control(self, text: str) -> bool:
        """Whether a short turn intentionally controls or redirects the call."""

        return (
            self._is_goodbye(text)
            or self._is_attention_check(text)
            or self._classify_permission(text) != "unknown"
        )

    def terminal_control_kind(self, text: str) -> str | None:
        """Return the explicit call-ending intent that must outrank task progress."""

        if self._is_goodbye(text):
            return "goodbye"
        if self._opening_attempted and self._classify_permission(text) == "refused":
            return "refusal"
        return None

    def note_opening_attempted(self) -> None:
        """Record dispatch of the opening before asynchronous audio events arrive.

        Realtime input transcription can complete before the interrupted greeting's
        ``response.done`` event. Recording this at dispatch time makes an immediate
        caller refusal authoritative even in that event ordering.
        """

        self._opening_attempted = True
        if self.call_context.direction.value == "inbound":
            self._permission_state = "granted"
            self._conversation_stage = "INTENT_DISCOVERY"
        else:
            self._conversation_stage = "AWAIT_PERMISSION"
        self._question_open = True
        self._refresh_live_state()

    def terminal_response_instruction(self, kind: str) -> str:
        """Create a short response-level instruction for an immediate clean close."""

        french = self.reply_language.lower().startswith("fr")
        if kind == "goodbye":
            exact = (
                "Merci pour votre temps. Au revoir." if french else "Thanks for your time. Goodbye."
            )
        else:
            exact = (
                "Aucun problème. Je ne vais pas vous retenir. Bonne journée."
                if french
                else "No problem. I won't keep you. Have a good day."
            )
        language = "French" if french else "English"
        return (
            f"The caller clearly chose to end the call ({kind}). Say exactly this once in "
            f"{language}, naturally and completely: {exact} Do not introduce yourself, mention "
            "the company or offer, ask a question, continue selling, or add any other words."
        )

    def last_spoken_turn(self) -> str:
        """The last thing the caller actually heard, for a repeat request."""

        return self._last_ai_response.strip()

    def note_repair_delivered(self) -> None:
        self._last_ai_delivery = "repair"
        self._refresh_live_state()

    def note_turn_understood(self) -> None:
        self.repair.record_success()

    def note_turn_quality(self, quality: TurnQuality) -> None:
        """Give the model acoustic/semantic context without choosing its words."""

        guidance = {
            TurnQuality.ACTIONABLE: (
                "Respond to the caller's actual meaning directly, then continue naturally."
            ),
            TurnQuality.BACKCHANNEL: (
                "Treat this brief response in the context of your last turn; do not launch a "
                "new script or assume details they did not say."
            ),
            TurnQuality.REPEAT_REQUEST: (
                "The caller is asking you to repeat or explain your last point. Rephrase it "
                "more simply and answer any clarification; do not move to a new question."
            ),
            TurnQuality.NOT_NOW: (
                "The caller may be directly saying this is a bad time. Verify that meaning from "
                "their actual sentence; quoted advice or a hypothetical callback is not a request."
            ),
            TurnQuality.IDENTITY_CHALLENGE: (
                "The caller wants to know who you are or why you called. Answer plainly and "
                "briefly before doing anything else."
            ),
            TurnQuality.FRAGMENT: (
                "The recognizer may have captured only part of the caller's thought. Use the "
                "actual transcript and context; if it truly remains incomplete, clarify briefly."
            ),
            TurnQuality.UNINTELLIGIBLE: (
                "The caller's audio was not intelligible. Do not infer meaning; ask them "
                "briefly and naturally to repeat it."
            ),
        }[quality]
        self._latest_turn_quality = quality.value
        self._latest_turn_guidance = guidance
        self._refresh_live_state()

    def _strip_self_name_vocative(self, sentence: str) -> str:
        """Stop the agent addressing the caller by its own name.

        The prompt says "You are Adam" and never names the caller, so the model
        filled the vocative slot with the only name it had: "That's great,
        Adam." to a customer who is not Adam. Unless the caller actually gave a
        name, no vocative is safer than the wrong one.
        """

        identity = self.persona_compiler.effective_identity
        own = str(identity.get("name", "")).strip()
        if not own:
            return sentence
        known = str((self.caller_memory or {}).get("name") or "").strip()
        if known and known.casefold() == own.casefold():
            return sentence
        first = re.escape(own.split()[0])
        cleaned = re.sub(rf",\s*{first}\b(?=[\s.,!?]|$)", "", sentence, flags=re.IGNORECASE)
        cleaned = re.sub(rf"^{first}\s*,\s*", "", cleaned, flags=re.IGNORECASE)
        if cleaned != sentence:
            logger.warning("Removed self-name vocative addressed to the caller")
        return cleaned.strip()

    def _is_repeat(self, sentence: str) -> bool:
        """True when this sentence was already spoken in this call."""

        if self._repeat_authorized_epoch == self._turn_epoch:
            return False

        normalized = " ".join(
            re.sub(r"[^\wÀ-ÿ\s]", " ", sentence.casefold(), flags=re.UNICODE).split()
        )
        if len(normalized) < 12:
            return False
        if normalized in self._spoken_sentences:
            return True
        tokens = set(normalized.split())
        if len(tokens) < 4:
            return False
        for previous in self._spoken_sentences:
            if len(normalized) >= 20 and (
                normalized in previous or previous in normalized
            ):
                return True
            if SequenceMatcher(None, normalized, previous).ratio() >= 0.88:
                return True
            other = set(previous.split())
            if not other:
                continue
            overlap = len(tokens & other) / max(len(tokens), len(other))
            if overlap >= 0.85:
                return True
        return False

    def _remember_spoken(self, sentence: str) -> None:
        normalized = " ".join(
            re.sub(r"[^\wÀ-ÿ\s]", " ", sentence.casefold(), flags=re.UNICODE).split()
        )
        if normalized:
            self._spoken_sentences.append(normalized)

    @property
    def turn_epoch(self) -> int:
        return self._turn_epoch

    def is_stale(self, epoch: int) -> bool:
        """True when the caller has spoken again since this reply began."""

        return epoch != self._turn_epoch

    async def observe_transcription(
        self,
        text: str,
        *,
        language_code: str | None = None,
        trusted_for_task: bool = True,
        transcription_confidence: float | None = None,
    ) -> None:
        self._turn_epoch += 1
        self.last_caller_text = text.strip()
        self._repeat_authorized_epoch = (
            self._turn_epoch
            if trusted_for_task and caller_authorizes_repetition(self.last_caller_text)
            else -1
        )
        self.last_caller_transcript_trusted = trusted_for_task
        self.last_caller_transcription_confidence = transcription_confidence
        self.recent_caller_turns.append((self.last_caller_text, trusted_for_task))
        detected = language_code or detect_language(self.last_caller_text)
        await self._adopt_caller_language(detected)
        register = detect_register(self.last_caller_text)
        if register:
            self._caller_register = register
        self._turn_started_at = time.monotonic()
        turn_quality = self.classify_turn(self.last_caller_text)
        if not trusted_for_task:
            turn_quality = TurnQuality.UNINTELLIGIBLE
        self.note_turn_quality(turn_quality)
        is_goodbye = self._is_goodbye(self.last_caller_text)
        is_attention_check = self._is_attention_check(self.last_caller_text)
        if (
            trusted_for_task
            and turn_quality is TurnQuality.ACTIONABLE
            and not (is_goodbye or is_attention_check)
        ):
            # Permission is interpreted by the conservative direct-intent
            # classifier below. A loose task-slot regex must never grant it
            # merely because a long sentence contains "okay" or "good time".
            actions = self.task.observe_caller_turn(
                self.last_caller_text,
                excluded_slots={"permission_to_continue"},
            )
            if actions.changed:
                logger.info(
                    "task state task_id=%s filled=%s stage=%s->%s",
                    self.task_id,
                    sorted(actions.state_delta),
                    actions.stage_from or self.task.stage,
                    actions.stage_to or self.task.stage,
                )
                await self._emit({"type": "task_state", **self.task.summary()})
        if is_goodbye:
            self.task.set_outcome(CallOutcome.REFUSED)
            self._permission_state = "refused"
            self._last_caller_intent = "goodbye"
            self._conversation_stage = "CLOSE"
        elif is_attention_check:
            self._last_caller_intent = "attention_check"
        elif not trusted_for_task:
            self._last_caller_intent = "uncertain_audio"
        elif turn_quality is not TurnQuality.ACTIONABLE:
            self._last_caller_intent = turn_quality.value
        elif self._opening_attempted:
            permission = self._classify_permission(self.last_caller_text)
            if permission != "unknown":
                if any(slot.id == "permission_to_continue" for slot in self.task.slots):
                    recorded = self.task.record("permission_to_continue", permission)
                    if recorded.changed:
                        await self._emit({"type": "task_state", **self.task.summary()})
                self._permission_state = permission
                self._last_caller_intent = f"permission_{permission}"
                self._conversation_stage = "DISCOVER" if permission == "granted" else "CLOSE"
            else:
                self._last_caller_intent = "new_information_or_question"
            if self._permission_state == "granted" and self.task.stage not in {"", "OPEN"}:
                self._conversation_stage = self.task.stage
        context_changed = False
        if trusted_for_task and turn_quality is TurnQuality.ACTIONABLE:
            context_changed = self.call_context.observe_caller_turn(
                self.last_caller_text,
                permission_state=self._permission_state,
            )
        if context_changed:
            await self._emit(
                {
                    "type": "call_context",
                    "direction": self.call_context.direction.value,
                    "mode": self.call_context.mode,
                    "phase": self.call_context.phase.value,
                    "interest": self.call_context.interest.value,
                    "product_qualification_unlocked": (
                        self.call_context.product_qualification_unlocked
                    ),
                }
            )
        self._refresh_live_state()
        event: dict[str, Any] = {
            "type": "transcript",
            "role": "user",
            "text": self.last_caller_text,
            "detected_language": self._caller_language,
        }
        if transcription_confidence is not None:
            event["transcription_confidence"] = round(transcription_confidence, 3)
        if not trusted_for_task:
            event["transcription_low_confidence"] = True
        await self._emit(event)

    async def finalize_response(
        self,
        raw_text: str,
        *,
        response_kind: str = "turn",
        enforce_spoken_policy: bool = True,
    ) -> tuple[str, TurnEvaluationResult]:
        if enforce_spoken_policy:
            spoken_text, policy_violations = PermissionGate.enforce_spoken_response(
                raw_text,
                language=self.reply_language,
                verified_actions=set(),
            )
        else:
            # Native speech-to-speech audio is already on the phone by the time
            # its transcript completes. Preserve the exact spoken text for UI,
            # evaluation, and memory instead of silently rewriting the record.
            spoken_text = raw_text.strip()
            policy_violations = []
        if response_kind == "greeting" and spoken_text:
            self.note_opening_attempted()
        if spoken_text:
            # The opening greeting normally ends in a question. Without this the
            # caller's "yes" was classified as an empty backchannel and dropped.
            self._question_open = spoken_text.rstrip().endswith("?")
            self._remember_spoken(spoken_text)
        self._last_ai_response = spoken_text
        self._last_ai_delivery = "generated"
        self._refresh_live_state()
        latency_ms = (
            (time.monotonic() - self._turn_started_at) * 1000 if self._turn_started_at else 0.0
        )
        evaluation = self.judge.evaluate_turn(
            caller_input=self.last_caller_text,
            ai_response=spoken_text,
            persona_data=self.persona_compiler.evaluation_persona_data,
            task_contract=self.task_contract,
            policy_violations=policy_violations,
            recent_ai_responses=tuple(self._completed_ai_turns),
        )
        if spoken_text:
            self._completed_ai_turns.append(spoken_text)
        metrics = {
            "turn_latency_ms": round(latency_ms, 1),
            "fidelity": evaluation.overall_score,
            "task_score": evaluation.task_performance_score * 4,
        }
        self._response_sequence += 1
        response_id = f"response-{self._response_sequence}"
        if spoken_text:
            self._pending_playback_ids.append(response_id)
        await self._emit(
            {
                "type": "transcript",
                "role": "assistant",
                "text": spoken_text,
                "metrics": metrics,
                "response_id": response_id,
                "response_kind": response_kind,
                "delivery_status": "generated",
            }
        )
        await self._emit(
            {
                "type": "evaluation",
                "score": evaluation.overall_score,
                "task_score": evaluation.task_performance_score * 4,
                "passed": evaluation.passed,
                "feedback": evaluation.feedback,
                "task_id": self.task_id,
            }
        )
        if self.memory_enabled and self.last_caller_text:
            task = asyncio.create_task(
                self.memory_writer.process_turn_async(
                    self.caller_id,
                    self.last_caller_text,
                    spoken_text,
                    latency_ms,
                    evaluation.overall_score,
                    self.task_id,
                    evaluation.feedback,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return spoken_text, evaluation

    def begin_streamed_response(self) -> str:
        """Reserve this turn's playback identity before any audio is produced.

        Streaming releases the first sentence to TTS while the model is still
        writing, so bot-speaking frames reach the playback reporter before the
        turn is finalized. The identity therefore has to exist up front.
        """

        self._response_sequence += 1
        response_id = f"response-{self._response_sequence}"
        self._pending_playback_ids.append(response_id)
        return response_id

    def guard_sentence(
        self,
        sentence: str,
        *,
        is_first: bool,
        response_kind: str = "turn",
    ) -> tuple[str, bool]:
        """Clear one sentence for speech before any of it can be heard.

        Returns the text to speak and whether the rest of the turn must be
        abandoned. Once a sentence has been spoken it cannot be recalled, so a
        guard that substitutes safe wording also ends the turn rather than
        letting the model continue from text the caller never heard.
        """

        self._last_guard_rejection = ""
        spoken, violations = PermissionGate.enforce_spoken_response(
            sentence,
            language=self.reply_language,
            verified_actions=set(),
        )
        spoken = self._strip_self_name_vocative(spoken)
        # Never let a known duplicate reach TTS. The response processor asks
        # the model to regenerate from the latest caller meaning; it does not
        # substitute a canned sales-stage sentence here.
        if self._is_repeat(spoken):
            self._last_guard_rejection = "repeat"
            logger.warning("Blocked model text repeated previously: %r", spoken[:80])
            return "", True
        if spoken:
            spoken = normalize_for_speech(spoken, self.reply_language)
            self._remember_spoken(spoken)
            self._question_open = spoken.rstrip().endswith("?")
            # Record it as it is released, not when the turn finalizes: the
            # caller can ask "hein ?" while the model is still writing, and the
            # repeat has to contain what they actually heard.
            self._last_ai_response = spoken
        return spoken, bool(violations)

    def consume_guard_rejection(self) -> str:
        """Return and clear the reason the latest sentence was rejected."""

        reason = self._last_guard_rejection
        self._last_guard_rejection = ""
        return reason

    async def finalize_streamed_response(
        self,
        response_id: str,
        spoken_text: str,
        *,
        response_kind: str = "turn",
    ) -> TurnEvaluationResult:
        """Record a turn whose sentences were already guarded and spoken."""

        if response_kind == "greeting" and spoken_text:
            self.note_opening_attempted()
        if spoken_text:
            self._question_open = spoken_text.rstrip().endswith("?")
        self._last_ai_response = spoken_text
        self._last_ai_delivery = "generated"
        self._refresh_live_state()
        latency_ms = (
            (time.monotonic() - self._turn_started_at) * 1000 if self._turn_started_at else 0.0
        )
        evaluation = self.judge.evaluate_turn(
            caller_input=self.last_caller_text,
            ai_response=spoken_text,
            persona_data=self.persona_compiler.evaluation_persona_data,
            task_contract=self.task_contract,
            policy_violations=[],
            recent_ai_responses=tuple(self._completed_ai_turns),
        )
        if spoken_text:
            self._completed_ai_turns.append(spoken_text)
        await self._emit(
            {
                "type": "transcript",
                "role": "assistant",
                "text": spoken_text,
                "metrics": {
                    "turn_latency_ms": round(latency_ms, 1),
                    "fidelity": evaluation.overall_score,
                    "task_score": evaluation.task_performance_score * 4,
                },
                "response_id": response_id,
                "response_kind": response_kind,
                "delivery_status": "generated",
            }
        )
        await self._emit(
            {
                "type": "evaluation",
                "score": evaluation.overall_score,
                "task_score": evaluation.task_performance_score * 4,
                "passed": evaluation.passed,
                "feedback": evaluation.feedback,
                "task_id": self.task_id,
            }
        )
        if self.memory_enabled and self.last_caller_text:
            task = asyncio.create_task(
                self.memory_writer.process_turn_async(
                    self.caller_id,
                    self.last_caller_text,
                    spoken_text,
                    latency_ms,
                    evaluation.overall_score,
                    self.task_id,
                    evaluation.feedback,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return evaluation

    def discard_pending_playback(self, response_id: str) -> None:
        """Drop a reserved identity when the turn produced nothing to speak."""

        try:
            self._pending_playback_ids.remove(response_id)
        except ValueError:
            pass

    def preview_response(self, raw_text: str) -> str:
        """Apply hard safety and TTS normalization without scripting dialogue."""

        spoken_text, _violations = PermissionGate.enforce_spoken_response(
            raw_text,
            language=self.reply_language,
            verified_actions=set(),
        )
        return normalize_for_speech(spoken_text, self.reply_language)

    async def playback_started(self) -> None:
        if self._active_playback_id is None and self._pending_playback_ids:
            self._active_playback_id = self._pending_playback_ids.popleft()
        self._playback_interrupted = False
        self._last_ai_delivery = "playing"
        self._refresh_live_state()
        if self._active_playback_id:
            await self._emit_playback_status("playing")

    async def mark_playback_interrupted(self) -> None:
        # Adopt a queued playback rather than reporting and closing it here.
        # Emitting "interrupted" and clearing the id made the later
        # playback_stopped() return early, so the operator lost the only figure
        # that says how much of the turn the caller actually heard.
        if self._active_playback_id is None and self._pending_playback_ids:
            self._active_playback_id = self._pending_playback_ids.popleft()
        if self._active_playback_id is not None:
            self._playback_interrupted = True
        self._last_ai_delivery = "interrupted"
        self._refresh_live_state()

    async def playback_stopped(
        self,
        *,
        delivered_frames: int | None = None,
        dropped_frames: int = 0,
    ) -> None:
        if self._active_playback_id is None:
            return
        message = ""
        if self._playback_interrupted:
            # A caller may barge in before Android renders the first frame. That
            # is a successful interruption, not a broken phone audio route.
            status = "interrupted"
            if delivered_frames is not None:
                message = f"Caller heard approximately {delivered_frames * 20 / 1000:.2f}s"
        elif delivered_frames is not None and delivered_frames <= 0:
            # Pipecat reports bot-speaking purely from TTS audio arriving at the
            # output transport, before any write is attempted. Reporting that as
            # "played" hid a completely dead uplink behind a confident success
            # message, so delivery is now judged by frames the phone link
            # actually accepted.
            status = "not_delivered"
            message = f"No audio reached the phone; {dropped_frames} output frame(s) were dropped"
            logger.error(
                "Assistant turn produced no delivered phone audio dropped_frames=%d",
                dropped_frames,
            )
        else:
            status = "completed"
        self._last_ai_delivery = status
        self._refresh_live_state()
        await self._emit_playback_status(status, message=message)
        self._active_playback_id = None
        self._playback_interrupted = False

    async def playback_failed(self, message: str) -> None:
        if self._active_playback_id is None and self._pending_playback_ids:
            self._active_playback_id = self._pending_playback_ids.popleft()
        if self._active_playback_id is None:
            return
        await self._emit_playback_status("failed", message=message)
        self._active_playback_id = None
        self._playback_interrupted = False
        self._last_ai_delivery = "failed"
        self._refresh_live_state()

    async def _emit_playback_status(self, status: str, *, message: str = "") -> None:
        await self._emit(
            {
                "type": "playback_status",
                "response_id": self._active_playback_id,
                "status": status,
                "message": message,
            }
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._active_playback_id is not None:
            await self._emit_playback_status("interrupted")
            self._active_playback_id = None
        while self._pending_playback_ids:
            self._active_playback_id = self._pending_playback_ids.popleft()
            await self._emit_playback_status("interrupted")
        self._active_playback_id = None
        if self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)
        if self.task.outcome is CallOutcome.IN_PROGRESS:
            # Nothing decided the ending, so classify it from what was actually
            # achieved rather than leaving the call unaccounted for.
            self.task.set_outcome(
                CallOutcome.REFUSED
                if self._permission_state == "refused"
                else CallOutcome.QUALIFIED
                if not self.task.missing_slots()
                else CallOutcome.ABANDONED
            )
        summary = self.task.summary()
        logger.info("call disposition %s", summary)
        await self._emit({"type": "call_outcome", **summary})
        if self.memory_enabled:
            await asyncio.to_thread(
                self.memory_manager.complete_call_session,
                self.caller_id,
                json.dumps(summary, ensure_ascii=False),
            )

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        result = self.event_sink(event)
        if inspect.isawaitable(result):
            await result


def transcription_evidence(
    frame: TranscriptionFrame,
) -> tuple[bool, float | None, str | None]:
    """Read project-owned acoustic metadata without trusting provider payloads."""

    result = frame.result if isinstance(frame.result, dict) else {}
    metadata = result.get("phone_agent", {}) if isinstance(result, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    trusted = metadata.get("trusted_for_task", True) is not False
    raw_confidence = metadata.get("confidence")
    confidence = (
        float(raw_confidence)
        if isinstance(raw_confidence, (int, float))
        else None
    )
    raw_language = metadata.get("language")
    language = str(raw_language).strip() if raw_language else None
    return trusted, confidence, language


class TranscriptionPolicyProcessor(FrameProcessor):
    """Observe final caller transcriptions without changing pipeline semantics."""

    def __init__(self, runtime: AgentPolicyRuntime) -> None:
        super().__init__()
        self.runtime = runtime

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction is FrameDirection.DOWNSTREAM and isinstance(frame, TranscriptionFrame):
            trusted, confidence, language = transcription_evidence(frame)
            await self.runtime.observe_transcription(
                frame.text,
                language_code=language,
                trusted_for_task=trusted,
                transcription_confidence=confidence,
            )
        await self.push_frame(frame, direction)


# A sentence ends at terminal punctuation followed by whitespace or the end of
# what has arrived so far. Decimals and abbreviations are deliberately not
# special-cased: releasing one clause early is cheap, and the run-on guard below
# bounds the damage if a model never punctuates.
_SENTENCE_BOUNDARY = re.compile(r"[^.!?…]*[.!?…]+(?:\s+|$)", re.UNICODE)
_RUN_ON_CHARS = 160


class ResponsePolicyProcessor(FrameProcessor):
    """Stream model-owned dialogue through narrow safety checks to speech.

    Buffering the whole response before speaking made the caller wait for the
    model to finish *and then* for the full utterance to be synthesized, which
    serialized two multi-second stages that Pipecat is designed to overlap.
    Each completed sentence receives only hard action-claim safety checks and
    TTS normalization, then is pushed immediately so synthesis of sentence one
    overlaps generation of sentence two. No task stage or canned conversation
    logic may replace the model's wording. The turn is recorded once.
    """

    def __init__(self, runtime: AgentPolicyRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self._pending = ""
        self._spoken: list[str] = []
        self._collecting = False
        self._stopped = False
        self._response_id: str | None = None
        self._epoch = -1
        self._rejected_repeat = ""
        self._repeat_retry: Callable[[str, int], Awaitable[bool]] | None = None
        self._retry_resolved: Callable[[int], Awaitable[None]] | None = None

    def bind_repetition_recovery(
        self,
        retry: Callable[[str, int], Awaitable[bool]],
        resolved: Callable[[int], Awaitable[None]],
    ) -> None:
        """Attach pipeline callbacks after its worker has been constructed."""

        self._repeat_retry = retry
        self._retry_resolved = resolved

    def _take_sentence(self) -> str | None:
        """Pop one complete sentence, or an early clause/bounded chunk of a reply."""

        match = _SENTENCE_BOUNDARY.match(self._pending)
        if match and match.group(0).strip():
            sentence = match.group(0)
            self._pending = self._pending[len(sentence) :]
            return sentence.strip()

        # If the sentence is not ready, allow an early clause for the first
        # chunk. Ignore a very short first comma ("I'm Adam,") and use the next
        # natural boundary instead; the old anchored regex then buffered the
        # entire introduction and serialized LLM generation with Kokoro.
        if not self._spoken:
            for boundary in re.finditer(r"[,;:]", self._pending):
                end = boundary.end()
                if end < 20:
                    continue
                if end > 72:
                    break
                if end == len(self._pending) or self._pending[end].isspace():
                    clause = self._pending[:end]
                    self._pending = self._pending[end:].lstrip()
                    return clause.strip()

        if len(self._pending) >= _RUN_ON_CHARS:
            cut = self._pending.rfind(" ", 0, _RUN_ON_CHARS)
            if cut <= 0:
                cut = _RUN_ON_CHARS
            sentence = self._pending[:cut]
            self._pending = self._pending[cut:].lstrip()
            if sentence.strip():
                return sentence.strip()
        return None

    async def _release(self, sentence: str, direction: FrameDirection) -> None:
        if self.runtime.is_stale(self._epoch):
            # The caller spoke again while this was being written. Answering the
            # older turn now would deliver two replies to one question.
            if not self._stopped:
                logger.warning("Dropped a reply superseded by a newer caller turn")
            self._stopped = True
            self._pending = ""
            return
        spoken, stop = self.runtime.guard_sentence(sentence, is_first=not self._spoken)
        rejection = self.runtime.consume_guard_rejection()
        if spoken:
            self._spoken.append(spoken)
            await self.push_frame(LLMTextFrame(spoken), direction)
        elif rejection == "repeat" and not self._spoken and not self._rejected_repeat:
            # Remember the duplicate in case the *whole* draft contains
            # nothing new.  Do not abandon the stream yet: smaller local
            # models often prefix a useful answer with one stale sentence.
            # Dropping only that sentence preserves the useful continuation
            # and avoids paying for another generation.
            self._rejected_repeat = sentence
        if stop:
            # Permission failures must abandon the remaining draft because it
            # may depend on wording the caller never heard.  Repetition is
            # different: skip that sentence and inspect later sentences for
            # genuinely new content.  If none exists, end-of-response recovery
            # schedules one clean regeneration.
            if rejection == "repeat":
                return
            self._stopped = True
            self._pending = ""

    def _reset(self) -> None:
        self._pending = ""
        self._spoken = []
        self._collecting = False
        self._stopped = False
        self._response_id = None
        self._epoch = -1
        self._rejected_repeat = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            await self.runtime.mark_playback_interrupted()
        if direction is FrameDirection.UPSTREAM and isinstance(frame, ErrorFrame):
            await self.runtime.playback_failed(frame.error)
        if direction is not FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, InterruptionFrame):
            if self._response_id is not None and not self._spoken:
                self.runtime.discard_pending_playback(self._response_id)
            self._reset()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset()
            self._collecting = True
            self._epoch = self.runtime.turn_epoch
            self._response_id = self.runtime.begin_streamed_response()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMTextFrame):
            if not self._collecting:
                self._collecting = True
                if self._response_id is None:
                    self._response_id = self.runtime.begin_streamed_response()
            if self._stopped:
                return
            self._pending += frame.text
            while (sentence := self._take_sentence()) is not None:
                await self._release(sentence, direction)
                if self._stopped:
                    break
            return
        if isinstance(frame, LLMFullResponseEndFrame) and self._collecting:
            trailing = self._pending.strip()
            self._pending = ""
            if trailing and not self._stopped:
                await self._release(trailing, direction)
            response_id = self._response_id
            spoken_text = " ".join(self._spoken).strip()
            stale = self.runtime.is_stale(self._epoch)
            epoch = self._epoch
            rejected_repeat = self._rejected_repeat
            self._reset()
            if stale and response_id is not None:
                self.runtime.discard_pending_playback(response_id)
                await self.push_frame(frame, direction)
                return
            if rejected_repeat and not spoken_text:
                if response_id is not None:
                    self.runtime.discard_pending_playback(response_id)
                # Close the rejected response before queuing the regenerated
                # one so frame lifecycles cannot overlap downstream.
                await self.push_frame(frame, direction)
                scheduled = False
                if self._repeat_retry is not None:
                    try:
                        scheduled = await self._repeat_retry(rejected_repeat, epoch)
                    except Exception:
                        logger.exception("Could not schedule repetition recovery")
                if not scheduled:
                    logger.error(
                        "Suppressed repeated response after recovery budget exhausted"
                    )
                return
            if response_id is not None:
                if spoken_text:
                    await self.runtime.finalize_streamed_response(response_id, spoken_text)
                else:
                    self.runtime.discard_pending_playback(response_id)
            if spoken_text and self._retry_resolved is not None:
                await self._retry_resolved(epoch)
            await self.push_frame(frame, direction)
            return
        await self.push_frame(frame, direction)


class PlaybackEventProcessor(FrameProcessor):
    """Report what the phone actually rendered, including interruptions.

    Bot-speaking frames say only that TTS produced audio; they are emitted
    before the transport attempts a write and are unaffected by its result.
    Delivery is therefore measured from the session's own transport counters
    across each speaking span.
    """

    def __init__(self, runtime: AgentPolicyRuntime, session: Any | None = None) -> None:
        super().__init__()
        self.runtime = runtime
        self._session = session
        self._delivered_at_start = 0
        self._dropped_at_start = 0

    def _counters(self) -> tuple[int, int]:
        if self._session is None:
            return 0, 0
        metrics = self._session.metrics
        return int(metrics.output_frames), int(metrics.dropped_output_frames)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            await self.runtime.mark_playback_interrupted()
        elif direction is FrameDirection.DOWNSTREAM:
            if isinstance(frame, BotStartedSpeakingFrame):
                self._delivered_at_start, self._dropped_at_start = self._counters()
                await self.runtime.playback_started()
            elif isinstance(frame, BotStoppedSpeakingFrame):
                delivered, dropped = self._counters()
                await self.runtime.playback_stopped(
                    delivered_frames=(
                        None if self._session is None else delivered - self._delivered_at_start
                    ),
                    dropped_frames=dropped - self._dropped_at_start,
                )
            elif isinstance(frame, ErrorFrame):
                await self.runtime.playback_failed(frame.error)
        await self.push_frame(frame, direction)
