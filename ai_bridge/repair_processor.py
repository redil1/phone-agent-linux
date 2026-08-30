"""Decide whether a caller turn is something to answer at all.

This sits between recognition and the language model. The model only ever sees
text, so it cannot tell that "Hello?" was the caller checking whether the line
is alive, that "Mm-hmm." was a backchannel, or that "I think." was a cough the
recognizer guessed at. On real calls it answered all three with a full sales
pitch, which is the clearest possible sign of a machine.

Turns carrying nothing to answer never reach the model. They are answered from
the persona's own wordings instead, which is both more controlled and faster -
a person reacts to not hearing something immediately, not after thinking.
"""

from __future__ import annotations

import logging

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .agent_policy import AgentPolicyRuntime
from .conversation_repair import TurnQuality

logger = logging.getLogger("PhoneAgentRepair")


class ConversationRepairProcessor(FrameProcessor):
    """Answer unclear turns from the persona; pass real turns to the model."""

    def __init__(self, runtime: AgentPolicyRuntime, *, enabled: bool = True) -> None:
        super().__init__()
        self.runtime = runtime
        self._enabled = enabled
        # A backchannel is by definition something said *over* the other
        # speaker. Once the agent has stopped and is waiting, anything the
        # caller says is a turn - dropping it leaves both sides silent.
        self._bot_speaking = False

    async def _speak(self, text: str, quality: TurnQuality) -> None:
        """Say a persona wording directly, without a model round trip."""

        if not text:
            return
        self.runtime.note_repair_delivered()
        logger.info("Repaired caller turn quality=%s chars=%d", quality.value, len(text))
        # append_to_context=False: a repair is not part of the conversation's
        # meaning, and letting it into context teaches the model to repeat it.
        await self.push_frame(
            TTSSpeakFrame(text, append_to_context=False), FrameDirection.DOWNSTREAM
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        await super().process_frame(frame, direction)
        if (
            not self._enabled
            or direction is not FrameDirection.DOWNSTREAM
            or not isinstance(frame, TranscriptionFrame)
        ):
            await self.push_frame(frame, direction)
            return

        quality = self.runtime.classify_turn(frame.text)

        if quality is TurnQuality.ACTIONABLE:
            self.runtime.note_turn_understood()
            await self.push_frame(frame, direction)
            return

        if quality is TurnQuality.BACKCHANNEL:
            if not self._bot_speaking:
                # The agent is waiting, so this is the caller's answer, not
                # filler over someone else's sentence. Dropping it here left
                # both sides silent with the turn invisible in the Studio.
                logger.info(
                    "Treated a short reply as an answer because the agent was not speaking: %r",
                    frame.text.strip()[:40],
                )
                self.runtime.note_turn_understood()
                await self.push_frame(frame, direction)
                return
            logger.warning(
                "Ignored caller backchannel over agent speech chars=%d",
                len(frame.text.strip()),
            )
            return

        if quality is TurnQuality.REPEAT_REQUEST:
            # They did not hear the last turn. Moving to new content ignores
            # that; repeat what was actually said.
            previous = self.runtime.last_spoken_turn()
            if previous:
                await self._speak(
                    f"{self.runtime.repair.repeat_preamble()} {previous}", quality
                )
            else:
                await self._speak(self.runtime.repair.next_repair(), quality)
            return

        if quality is TurnQuality.NOT_NOW:
            await self._speak(self.runtime.repair.not_now_reply(), quality)
            return

        if quality is TurnQuality.IDENTITY_CHALLENGE:
            identity = self.runtime.persona_compiler.effective_identity
            await self._speak(
                self.runtime.repair.identity_reply(
                    name=str(identity.get("name", "")),
                    company=str(identity.get("company", "OXzoon")),
                ),
                quality,
            )
            return

        # FRAGMENT or UNINTELLIGIBLE: ask again, escalating each consecutive
        # time rather than repeating one apology.
        await self._speak(self.runtime.repair.next_repair(), quality)
