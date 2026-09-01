"""Direction-aware advisory context for natural model-led conversation.

Permission to continue a cold outbound call is not product interest.  This
small state tracker keeps that distinction visible to the model without
rewriting, rejecting, or substituting anything the model says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class CallDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class ProspectingPhase(StrEnum):
    AWAIT_PERMISSION = "await_permission"
    RELEVANCE_DISCOVERY = "relevance_discovery"
    NEED_DEVELOPMENT = "need_development"
    INTEREST_CHECK = "interest_check"
    PRODUCT_QUALIFICATION = "product_qualification"
    INTENT_DISCOVERY = "intent_discovery"
    CLOSE = "close"


class InterestState(StrEnum):
    UNKNOWN = "unknown"
    CALLER_INITIATED = "caller_initiated"
    NEED_SIGNAL = "need_signal"
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"


_PERMISSION_ONLY = re.compile(
    r"^(?:yes|yeah|yep|sure|okay|ok|go ahead|oui|ouais|d'accord|allez-y)[.! ]*$",
    re.IGNORECASE,
)
_NOT_INTERESTED = re.compile(
    r"\b(?:not interested|no interest|don't want|do not want|stop calling|"
    r"pas intéressé|pas interessee|pas intéressée|ça ne m'intéresse pas|"
    r"cela ne m'intéresse pas|ne m'appelez plus)\b",
    re.IGNORECASE,
)
_EXPLICIT_INTEREST = re.compile(
    r"\b(?:i(?:'m| am) interested|tell me more|sounds (?:good|useful|interesting)|"
    r"that (?:could|would) help|(?:that|it) is interesting|would be interesting|worth exploring|"
    r"i(?:'d| would) like to know more|what do you offer|how much|what(?:'s| is) the price|"
    r"i need (?:that|this|something)|whatsapp|send (?:me )?(?:the )?(?:offer|details|info|message)|"
    r"je suis intéressé|ça m'intéresse|envoyez(?:-moi)?|"
    r"dites-m'en plus|combien|quel est le prix|qu'est-ce que vous proposez)\b",
    re.IGNORECASE,
)
_NEED_SIGNAL = re.compile(
    r"\b(?:buffer|freez|slow|expensive|cost|price|limited|missing|problem|issue|"
    r"channels?|sports?|football|movies?|series|streaming|subscription|tv|television|"
    r"cable|firestick|"
    r"smart tv|mobile|phone|freeze|cher|coût|prix|lent|problème|chaînes?|sport|"
    r"films?|séries?|abonnement|télévision)\b",
    re.IGNORECASE,
)
def normalize_direction(value: str | CallDirection) -> CallDirection:
    try:
        return CallDirection(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError("call direction must be inbound or outbound") from exc


@dataclass(slots=True)
class CallContextPolicy:
    """Track cold-prospecting readiness separately from task slots."""

    direction: CallDirection | str
    phase: ProspectingPhase | None = None
    interest: InterestState | None = None
    substantive_turns: int = 0

    def __post_init__(self) -> None:
        self.direction = normalize_direction(self.direction)
        if self.direction is CallDirection.INBOUND:
            self.phase = ProspectingPhase.INTENT_DISCOVERY
            self.interest = InterestState.CALLER_INITIATED
        else:
            self.phase = ProspectingPhase.AWAIT_PERMISSION
            self.interest = InterestState.UNKNOWN

    @property
    def product_qualification_unlocked(self) -> bool:
        return self.direction is CallDirection.INBOUND or self.interest is InterestState.INTERESTED

    @property
    def mode(self) -> str:
        return "cold_prospecting" if self.direction is CallDirection.OUTBOUND else "intent_led"

    def observe_caller_turn(self, text: str, *, permission_state: str) -> bool:
        """Advance prospecting state from the caller's explicit words."""

        before = (self.phase, self.interest, self.substantive_turns)
        rendered = " ".join(str(text or "").strip().split())
        if self.direction is CallDirection.INBOUND:
            return False
        if permission_state == "refused" or _NOT_INTERESTED.search(rendered):
            self.interest = InterestState.NOT_INTERESTED
            self.phase = ProspectingPhase.CLOSE
        elif _EXPLICIT_INTEREST.search(rendered):
            self.interest = InterestState.INTERESTED
            self.phase = ProspectingPhase.PRODUCT_QUALIFICATION
        elif permission_state != "granted":
            self.phase = ProspectingPhase.AWAIT_PERMISSION
        elif self.phase is ProspectingPhase.INTEREST_CHECK and _PERMISSION_ONLY.fullmatch(rendered):
            self.interest = InterestState.INTERESTED
            self.phase = ProspectingPhase.PRODUCT_QUALIFICATION
        elif _PERMISSION_ONLY.fullmatch(rendered):
            self.phase = ProspectingPhase.RELEVANCE_DISCOVERY
        else:
            self.substantive_turns += 1
            if _NEED_SIGNAL.search(rendered):
                self.interest = InterestState.NEED_SIGNAL
            if self.substantive_turns >= 2:
                self.phase = ProspectingPhase.INTEREST_CHECK
            else:
                self.phase = ProspectingPhase.NEED_DEVELOPMENT
        return before != (self.phase, self.interest, self.substantive_turns)

    def base_instructions(self) -> str:
        if self.direction is CallDirection.INBOUND:
            return """# CALL DIRECTION — INBOUND INTENT-LED
- The caller initiated this call, which is a real signal of intent.
- Confirm why they called and what outcome they want, then move directly into the relevant task.
- Do not force a cold-sales permission script onto an inbound caller.
- Still verify facts, listen first, and never assume the exact product or action they want."""
        return """# CALL DIRECTION — OUTBOUND COLD PROSPECTING
- You initiated this call to the prospect. Never ask why they called or what made them decide
  to call, because YOU are the one who dialed them.
- Permission to continue means only “you may speak”; it NEVER means interest, need,
  or buying intent.
- Treat the sequence below as ethical strategy, not as a script or a reason to dodge the caller.
- Answer direct questions, corrections, and requests for clarification before returning naturally
  to discovery.
- Usually establish relevance before asking about device, package, price, plan, trial, payment,
  or setup.
- Discover a real frustration, limitation, desired result, cost, risk, or missed opportunity.
- Create demand ethically: reflect their own situation, connect it to one verified useful outcome,
  and let them decide whether that outcome is worth exploring.
- Prefer to establish genuine interest before fit questions such as device, package, budget,
  channels, or setup, unless the caller asks about one of those topics first.
- Never manufacture a problem, use fake urgency, pressure, or treat politeness as consent.
- A clear lack of interest ends selling immediately and respectfully."""

    def steering(self, task_question: str) -> tuple[str, str]:
        """Return an optional strategy hint and an optional task topic."""

        if self.direction is CallDirection.INBOUND:
            return (
                "Confirm the caller's reason and desired outcome, then use the task question.",
                task_question,
            )
        if self.phase is ProspectingPhase.AWAIT_PERMISSION:
            return (
                "Listen for the answer to the opening permission question, while still "
                "answering anything the caller asks.",
                task_question,
            )
        if self.phase is ProspectingPhase.RELEVANCE_DISCOVERY:
            return (
                "Ask one open, non-product question about how they currently handle this area.",
                task_question,
            )
        if self.phase is ProspectingPhase.NEED_DEVELOPMENT:
            return (
                "Reflect their situation, then ask what they would most like to improve or avoid.",
                task_question,
            )
        if self.phase is ProspectingPhase.INTEREST_CHECK:
            return (
                "Connect their stated need to one verified outcome, then ask whether "
                "it is worth exploring.",
                task_question,
            )
        if self.phase is ProspectingPhase.CLOSE:
            return ("Close politely with no further sales question.", task_question)
        return ("Interest is explicit; continue with task qualification.", task_question)

    def state_block(self, task_question: str) -> str:
        move, suggested_question = self.steering(task_question)
        return (
            f"call_direction: {self.direction.value}\n"
            f"conversation_mode: {self.mode}\n"
            f"prospecting_phase: {self.phase.value}\n"
            f"prospect_interest: {self.interest.value}\n"
            f"explicit_product_interest_observed: "
            f"{'yes' if self.product_qualification_unlocked else 'no'}\n"
            f"conversation_strategy_hint: {move}\n"
            f"optional_task_topic: {suggested_question}"
        )

    def opening_greeting(
        self,
        *,
        name: str,
        role: str,
        language: str,
        configured_outbound: str = "",
    ) -> str:
        """Choose an outbound permission opening or an inbound welcome."""

        if self.direction is CallDirection.OUTBOUND and configured_outbound.strip():
            return configured_outbound.strip().replace("{name}", name)
        french = str(language).lower().startswith("fr")
        company = role.rsplit(" at ", 1)[-1].strip() if " at " in role else ""
        if self.direction is CallDirection.INBOUND:
            if french:
                represented = f" d{chr(8217)}{company}" if company else ""
                return (
                    f"Bonjour, ici {name}{represented}. Merci de nous appeler. "
                    "Comment puis-je vous aider ?"
                )
            represented = f" from {company}" if company else ""
            return f"Hello, this is {name}{represented}. Thanks for calling. How can I help?"
        if french:
            represented = f" d{chr(8217)}{company}" if company else ""
            return f"Bonjour, ici {name}{represented}. Est-ce un bon moment pour une question ?"
        represented = f" from {company}" if company else ""
        return f"Hello, this is {name}{represented}. Is now a good time for one question?"
