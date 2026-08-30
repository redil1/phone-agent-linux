from __future__ import annotations

import numpy as np
from phone_agent_gateway.ai_bridge.duplex_echo_gate import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    DuplexEchoGate,
)


def _speech_like(seed: int, frames: int = 60) -> np.ndarray:
    rng = np.random.default_rng(seed)
    count = frames * FRAME_SAMPLES
    excitation = rng.normal(0.0, 1.0, count)
    kernel_time = np.arange(96) / 16_000
    kernel = np.exp(-np.arange(96) / 22) * (
        np.sin(2 * np.pi * 620 * kernel_time)
        + 0.55 * np.sin(2 * np.pi * 1_350 * kernel_time)
        + 0.25 * np.sin(2 * np.pi * 2_400 * kernel_time)
    )
    speech = np.convolve(excitation, kernel, mode="same")
    envelope = 0.2 + 0.8 * np.sin(np.linspace(0, 9 * np.pi, count)) ** 2
    speech *= envelope
    speech *= 8_000 / max(1.0, float(np.max(np.abs(speech))))
    return speech.astype("<i2")


def _frames(samples: np.ndarray):
    for offset in range(0, samples.size, FRAME_SAMPLES):
        frame = samples[offset : offset + FRAME_SAMPLES]
        if frame.size == FRAME_SAMPLES:
            yield frame.astype("<i2", copy=False).tobytes()


def test_correlated_far_end_echo_is_replaced_with_timing_preserving_silence() -> None:
    now = 100.0
    gate = DuplexEchoGate(clock=lambda: now)
    assistant = _speech_like(7)
    for frame in _frames(assistant):
        gate.note_output_pcm(frame)
    gate.set_playback_active(True)

    rng = np.random.default_rng(9)
    filtered = np.convolve(
        assistant.astype(np.float64), np.array([0.55, 0.25, 0.12, 0.06]), mode="same"
    )
    echo = filtered * 0.2 + rng.normal(0.0, 12.0, assistant.size)
    echo = np.concatenate((np.zeros(113), echo))[: assistant.size].astype("<i2")
    emitted: list[bytes] = []
    for frame in _frames(echo):
        now += 0.02
        emitted.extend(gate.process_input_frame(frame))

    snapshot = gate.snapshot()
    assert snapshot["max_echo_correlation"] >= 0.72
    assert snapshot["echo_suppressed_frames"] >= 45
    assert sum(frame == b"\x00" * FRAME_BYTES for frame in emitted) >= 45
    assert gate.has_recent_human_speech() is False


def test_uncorrelated_caller_barge_in_passes_while_assistant_is_playing() -> None:
    now = 200.0
    gate = DuplexEchoGate(clock=lambda: now)
    for frame in _frames(_speech_like(11)):
        gate.note_output_pcm(frame)
    gate.set_playback_active(True)

    caller = _speech_like(29, frames=20)
    emitted: list[bytes] = []
    for frame in _frames(caller):
        now += 0.02
        emitted.extend(gate.process_input_frame(frame))

    non_silent = sum(frame != b"\x00" * FRAME_BYTES for frame in emitted)
    assert non_silent >= 16
    assert gate.has_recent_human_speech() is True


def test_quiet_audio_is_untouched_when_assistant_is_not_playing() -> None:
    gate = DuplexEchoGate()
    t = np.arange(FRAME_SAMPLES) / 16_000
    quiet = (np.sin(2 * np.pi * 440 * t) * 35).astype("<i2").tobytes()

    assert gate.process_input_frame(quiet) == [quiet]
    assert gate.snapshot()["echo_suppressed_frames"] == 0
