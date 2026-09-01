"""Annotate caller-turn quality without scripting the AI's response.

This sits between recognition and the language model. The model only ever sees
text, so it cannot tell that "Hello?" was the caller checking whether the line
is alive, that "Mm-hmm." was a backchannel, or that "I think." was a cough the
recognizer guessed at. On real calls it answered all three with a full sales
pitch, which is the clearest possible sign of a machine.

The model receives every meaningful turn plus a live-system hint describing
whether it sounded actionable, incomplete, unintelligible, or like a request to
repeat. Only a true backchannel spoken over the agent is discarded. This keeps
acoustic knowledge outside the LLM while leaving all conversational wording and
reasoning inside it.
"""

from __future__ import annotations

import logging

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .agent_policy import AgentPolicyRuntime, transcription_evidence
from .conversation_repair import TurnQuality

logger = logging.getLogger("PhoneAgentRepair")


class ConversationRepairProcessor(FrameProcessor):
    """Give the LLM turn-quality context and otherwise stay out of dialogue."""

    def __init__(self, runtime: AgentPolicyRuntime, *, enabled: bool = True) -> None:
        super().__init__()
        self.runtime = runtime
        self._enabled = enabled
        # A backchannel is by definition something said *over* the other
        # speaker. Once the agent has stopped and is waiting, anything the
        # caller says is a turn - dropping it leaves both sides silent.
        self._bot_speaking = False

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

        trusted, _, _ = transcription_evidence(frame)
        quality = (
            self.runtime.classify_turn(frame.text)
            if trusted
            else TurnQuality.UNINTELLIGIBLE
        )
        self.runtime.note_turn_quality(quality)

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

        # Repeat requests, bad timing, identity challenges, fragments, and
        # unintelligible audio all need contextual reasoning. Pass the caller's
        # words through and let the model respond using the quality guidance in
        # the mutable live-state system message.
        logger.info(
            "Delegated caller-turn recovery to model quality=%s chars=%d",
            quality.value,
            len(frame.text.strip()),
        )
        await self.push_frame(frame, direction)
