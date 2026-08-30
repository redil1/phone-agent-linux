"""Tests for the local Parakeet speech recognition service.

The model itself is stubbed. What matters here is the turn contract the
pipeline depends on: exactly one authoritative caller turn per utterance,
correct start/stop speaking frames, endpointing that never fires on silence
alone, and no wasted second inference pass when speculation already
transcribed the identical audio.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest
from phone_agent_gateway.ai_bridge import parakeet_local_stt
from phone_agent_gateway.ai_bridge.parakeet_local_stt import ParakeetLocalSTTService
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
)

SAMPLE_RATE = 16_000


def _pcm(milliseconds: int, amplitude: int) -> bytes:
    count = SAMPLE_RATE * milliseconds // 1000
    return struct.pack(f"<{count}h", *([amplitude] * count))


LOUD = lambda ms: _pcm(ms, 8000)  # noqa: E731 - well above the -42 dBFS gate
SILENT = lambda ms: _pcm(ms, 0)  # noqa: E731


class _Harness:
    """Collect the frames the service pushes downstream."""

    def __init__(self, service: ParakeetLocalSTTService) -> None:
        self.frames: list[Any] = []
        service.push_frame = self._capture  # type: ignore[method-assign]

    async def _capture(self, frame: Any, direction: Any = None) -> None:
        self.frames.append(frame)

    def types(self) -> list[str]:
        return [type(f).__name__ for f in self.frames]

    def transcriptions(self) -> list[str]:
        return [f.text for f in self.frames if isinstance(f, TranscriptionFrame)]


async def _feed(service: ParakeetLocalSTTService, pcm: bytes, chunk_ms: int = 20) -> None:
    step = SAMPLE_RATE * 2 * chunk_ms // 1000
    for offset in range(0, len(pcm), step):
        async for _ in service.run_stt(pcm[offset : offset + step]):
            pass


async def _make(monkeypatch: pytest.MonkeyPatch, text: str, **kwargs: Any):
    calls: list[int] = []

    def fake_transcribe(pcm: bytes, model_id: str = "") -> str:
        calls.append(len(pcm))
        return text

    monkeypatch.setattr(parakeet_local_stt, "transcribe_pcm", fake_transcribe)
    monkeypatch.setattr(parakeet_local_stt, "load_model", lambda model_id=None: object())

    service = ParakeetLocalSTTService(
        sample_rate=SAMPLE_RATE,
        endpoint_ms=kwargs.pop("endpoint_ms", 200),
        incomplete_endpoint_ms=kwargs.pop("incomplete_endpoint_ms", 400),
        prefetch_silence_ms=kwargs.pop("prefetch_silence_ms", 60),
        **kwargs,
    )
    harness = _Harness(service)
    await service.start(
        StartFrame(audio_in_sample_rate=SAMPLE_RATE, audio_out_sample_rate=SAMPLE_RATE)
    )
    return service, harness, calls


@pytest.mark.asyncio
async def test_commits_one_turn_after_local_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    service, harness, _ = await _make(monkeypatch, "I would like the sports package")
    try:
        await _feed(service, LOUD(300))
        await _feed(service, SILENT(200))
        await asyncio.sleep(0.4)
    finally:
        await service.cleanup()

    assert harness.transcriptions() == ["I would like the sports package"]
    order = harness.types()
    assert order.index("UserStartedSpeakingFrame") < order.index("TranscriptionFrame")
    assert order.index("TranscriptionFrame") < order.index("UserStoppedSpeakingFrame")


@pytest.mark.asyncio
async def test_silence_alone_never_produces_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle line must not manufacture caller turns for the LLM."""

    service, harness, calls = await _make(monkeypatch, "phantom")
    try:
        await _feed(service, SILENT(600))
        await asyncio.sleep(0.4)
    finally:
        await service.cleanup()

    assert harness.transcriptions() == []
    assert calls == [], "silence must never reach the recognizer"


@pytest.mark.asyncio
async def test_speculative_transcript_is_reused_for_the_final_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same buffered audio must not be transcribed twice."""

    service, harness, calls = await _make(
        monkeypatch, "the sports package please", speculative_pipeline_enabled=True
    )
    seen: list[str] = []
    service.set_speculation_handlers(lambda text: seen.append(text), None)
    try:
        await _feed(service, LOUD(300))
        await _feed(service, SILENT(200))
        await asyncio.sleep(0.5)
    finally:
        await service.cleanup()

    assert harness.transcriptions() == ["the sports package please"]
    assert any(isinstance(f, InterimTranscriptionFrame) for f in harness.frames)
    assert seen == ["the sports package please"]
    # One speculative pass; the commit reuses it rather than paying for another.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_incomplete_fragment_waits_longer_before_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing conjunction means the caller is mid-thought."""

    service, harness, _ = await _make(
        monkeypatch,
        "I want the package because",
        speculative_pipeline_enabled=True,
        endpoint_ms=200,
        incomplete_endpoint_ms=1500,
    )
    try:
        await _feed(service, LOUD(300))
        await _feed(service, SILENT(200))
        # Past the normal endpoint, but this transcript trails off.
        await asyncio.sleep(0.45)
        assert harness.transcriptions() == []
    finally:
        await service.cleanup()


@pytest.mark.asyncio
async def test_empty_transcript_does_not_emit_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    service, harness, _ = await _make(monkeypatch, "   ")
    try:
        await _feed(service, LOUD(300))
        await _feed(service, SILENT(200))
        await asyncio.sleep(0.4)
    finally:
        await service.cleanup()

    assert harness.transcriptions() == []
    assert "UserStoppedSpeakingFrame" in harness.types()


@pytest.mark.asyncio
async def test_rejects_languages_outside_the_call_policy() -> None:
    with pytest.raises(ValueError, match="English and French"):
        ParakeetLocalSTTService(sample_rate=SAMPLE_RATE, language="ar-MA")


def test_dbfs_distinguishes_speech_from_silence() -> None:
    assert parakeet_local_stt._calc_dbfs(SILENT(20)) == -120.0
    assert parakeet_local_stt._calc_dbfs(LOUD(20)) > -42.0


# ----------------------------------------- regression from the 19:22 call
# Two caller turns appeared that the operator never spoke: "I think I'm not
# sure." during the greeting, and "Yes." during the agent's own reply. Both
# audio windows coincided exactly with the agent speaking, and both produced
# far too few characters for their length.

def _service(**kwargs: Any) -> ParakeetLocalSTTService:
    return ParakeetLocalSTTService(sample_rate=SAMPLE_RATE, **kwargs)


@pytest.mark.parametrize(
    ("text", "audio_ms", "discarded"),
    [
        # The two phantom turns.
        ("I think I'm not sure.", 4840, True),
        ("Yes.", 4300, True),
        # Genuine turns that must survive.
        ("Yes.", 820, False),
        ("Oui.", 600, False),
        ("Je regarde surtout le sport et les films.", 3200, False),
        ("Yes, absolutely, that sounds good.", 3400, False),
    ],
)
def test_transcripts_too_short_for_their_audio_are_discarded(
    text: str, audio_ms: int, discarded: bool
) -> None:
    service = _service()
    audio_bytes = SAMPLE_RATE * 2 * audio_ms // 1000
    assert service._looks_hallucinated(text, audio_bytes) is discarded


def test_a_hallucinated_turn_never_reaches_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Five seconds of echo must not become a caller turn."""

    async def scenario() -> None:
        service, harness, _ = await _make(
            monkeypatch, "I think I'm not sure.", endpoint_ms=200
        )
        try:
            # Long stretch of above-threshold audio, as returning echo produces.
            await _feed(service, LOUD(4800))
            await _feed(service, SILENT(300))
            await asyncio.sleep(0.5)
        finally:
            await service.cleanup()
        assert harness.transcriptions() == []

    asyncio.run(scenario())


def test_the_echo_gate_is_disabled_by_default() -> None:
    """It deafened live calls, and the implausibility guard already covers echo."""

    assert _service()._echo_guard_db == 0.0


def test_bot_speaking_state_tracks_the_transport() -> None:
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection as _Direction

    async def scenario() -> None:
        service = _service(echo_guard_db=10.0)
        service.push_frame = _noop  # type: ignore[method-assign]
        assert service._bot_speaking is False

        await service.process_frame(BotStartedSpeakingFrame(), _Direction.UPSTREAM)
        assert service._bot_speaking is True

        await service.process_frame(BotStoppedSpeakingFrame(), _Direction.UPSTREAM)
        assert service._bot_speaking is False

    asyncio.run(scenario())


def test_a_missed_stop_frame_cannot_deafen_the_call() -> None:
    """A wedged gate silently ignored the caller for the rest of the call."""

    from pipecat.frames.frames import BotStartedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection as _Direction

    async def scenario() -> None:
        service = _service(echo_guard_db=10.0)
        service.push_frame = _noop  # type: ignore[method-assign]
        await service.process_frame(BotStartedSpeakingFrame(), _Direction.UPSTREAM)
        assert service._bot_speaking is True

        # No stop frame ever arrives; pretend the utterance began long ago.
        service._bot_speaking_since -= 60.0
        async for _ in service.run_stt(LOUD(20)):
            pass

        assert service._bot_speaking is False, "the gate must clear itself"

    asyncio.run(scenario())


async def _noop(frame: Any, direction: Any = None) -> None:
    return None
