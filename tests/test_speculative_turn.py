"""Tests for one-candidate speculative turn orchestration."""

from __future__ import annotations

import asyncio

import pytest
from phone_agent_gateway.ai_bridge.speculative_turn import SpeculativeTurnCoordinator
from pipecat.processors.aggregators.llm_context import LLMContext


class _FakeLLM:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.contexts: list[LLMContext] = []

    def start_prefetch(self, context: LLMContext) -> asyncio.Task[str]:
        self.contexts.append(context)

        async def complete() -> str:
            return "A concise answer."

        return asyncio.create_task(complete())

    def cancel_prefetch(self, reason: str) -> None:
        self.cancelled.append(reason)


class _FakeTTS:
    def __init__(self) -> None:
        self.prefetched: list[str] = []
        self.clear_count = 0

    async def prefetch_text(self, text: str) -> None:
        self.prefetched.append(text)

    def clear_prefetch(self) -> None:
        self.clear_count += 1


class _FakePolicy:
    def preview_response(self, text: str) -> str:
        return text


@pytest.mark.asyncio
async def test_coordinator_prefetches_one_context_bound_candidate() -> None:
    context = LLMContext(messages=[{"role": "system", "content": "Be concise."}])
    llm = _FakeLLM()
    tts = _FakeTTS()
    coordinator = SpeculativeTurnCoordinator(
        context=context,
        llm=llm,
        tts=tts,
        policy=_FakePolicy(),  # type: ignore[arg-type]
    )

    await coordinator.consider("Can you help me?")
    assert coordinator._task is not None
    await coordinator._task

    assert llm.contexts[0].get_messages()[-1] == {
        "role": "user",
        "content": "Can you help me?",
    }
    assert tts.prefetched == ["A concise answer."]


@pytest.mark.asyncio
async def test_coordinator_revision_clears_stale_text_and_audio() -> None:
    coordinator = SpeculativeTurnCoordinator(
        context=LLMContext(),
        llm=_FakeLLM(),
        tts=_FakeTTS(),
        policy=_FakePolicy(),  # type: ignore[arg-type]
    )

    await coordinator.consider("First revision")
    await coordinator.consider("Corrected revision")

    assert coordinator._candidate == "Corrected revision"
    assert coordinator._tts.clear_count >= 2
    await coordinator.close()
