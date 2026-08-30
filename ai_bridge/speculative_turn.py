"""Safe one-candidate look-ahead for low-latency conversational turns."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

from pipecat.processors.aggregators.llm_context import LLMContext

from .agent_policy import AgentPolicyRuntime, EventSink

logger = logging.getLogger("PhoneAgentSpeculativeTurn")


class SpeculativeTurnCoordinator:
    """Prefetch Gemini text and exact Andrew PCM, but never speak before commit."""

    def __init__(
        self,
        *,
        context: LLMContext,
        llm: Any,
        tts: Any,
        policy: AgentPolicyRuntime,
        event_sink: EventSink | None = None,
    ) -> None:
        self._context = context
        self._llm = llm
        self._tts = tts
        self._policy = policy
        self._event_sink = event_sink
        self._candidate = ""
        self._revision = 0
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def supported(self) -> bool:
        return all(
            callable(getattr(service, method, None))
            for service, method in (
                (self._llm, "start_prefetch"),
                (self._llm, "cancel_prefetch"),
                (self._tts, "prefetch_text"),
                (self._tts, "clear_prefetch"),
            )
        )

    async def consider(self, transcript: str) -> None:
        if self._closed or not self.supported:
            return
        normalized = " ".join(transcript.split())
        if not normalized or normalized == self._candidate:
            return
        await self.cancel("transcript_revised")
        self._candidate = normalized
        self._revision += 1
        revision = self._revision
        messages = [dict(message) for message in self._context.get_messages()]
        messages.append({"role": "user", "content": normalized})
        speculative_context = LLMContext(messages=messages)
        self._task = asyncio.create_task(
            self._run_candidate(revision, normalized, speculative_context),
            name="phoneagent-speculative-turn",
        )

    async def _run_candidate(
        self,
        revision: int,
        transcript: str,
        context: LLMContext,
    ) -> None:
        started = time.monotonic()
        try:
            llm_task = self._llm.start_prefetch(context)
            if llm_task is None:
                await self._emit("unsupported", transcript, started)
                return
            raw_text = await llm_task
            if revision != self._revision or transcript != self._candidate:
                return
            spoken_text = self._policy.preview_response(raw_text)
            if not spoken_text:
                return
            llm_ready = time.monotonic()
            await self._tts.prefetch_text(spoken_text)
            if revision != self._revision or transcript != self._candidate:
                return
            await self._emit(
                "ready",
                transcript,
                started,
                llm_ms=(llm_ready - started) * 1000,
                tts_ms=(time.monotonic() - llm_ready) * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("speculative candidate failed; normal path remains active", exc_info=True)
            await self._emit("failed", transcript, started)

    async def cancel(self, reason: str) -> None:
        task = self._task
        self._task = None
        self._revision += 1
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        cancel_llm = getattr(self._llm, "cancel_prefetch", None)
        if callable(cancel_llm):
            cancel_llm(reason)
        clear_tts = getattr(self._tts, "clear_prefetch", None)
        if callable(clear_tts):
            clear_tts()
        if self._candidate:
            logger.debug("cancelled speculative turn reason=%s", reason)
        self._candidate = ""

    async def close(self) -> None:
        self._closed = True
        await self.cancel("coordinator_closed")

    async def _emit(
        self,
        state: str,
        transcript: str,
        started: float,
        **timings: float,
    ) -> None:
        if self._event_sink is None:
            return
        event: dict[str, object] = {
            "type": "speculation",
            "state": state,
            "candidate_chars": len(transcript),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
        event.update({key: round(value, 1) for key, value in timings.items()})
        result = self._event_sink(event)
        if inspect.isawaitable(result):
            await result
