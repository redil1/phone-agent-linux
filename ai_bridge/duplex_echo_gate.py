"""Low-latency far-end echo rejection for the cellular full-duplex path.

The Android bridge captures the cellular downlink while assistant audio is sent
to the uplink. A remote handset or carrier can return a delayed, filtered copy
of that assistant audio on the downlink. Server VAD otherwise treats that copy
as a caller interruption. This gate compares incoming PCM with the exact PCM
recently accepted by the phone uplink and suppresses only strongly correlated
audio. Uncorrelated caller speech continues immediately.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2


@dataclass(slots=True)
class _PendingInput:
    pcm: bytes
    rms: float
    force_suppress: bool = False


class DuplexEchoGate:
    """Reject delayed assistant echo with one 20 ms frame of look-ahead."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        reference_secs: float = 2.0,
        echo_tail_secs: float = 1.2,
        correlation_threshold: float = 0.72,
        minimum_echo_rms: float = 18.0,
        low_noise_rms: float = 35.0,
        human_speech_rms: float = 45.0,
        human_speech_frames: int = 3,
    ) -> None:
        self._clock = clock
        self._reference_frames: deque[np.ndarray] = deque(
            maxlen=max(10, math.ceil(reference_secs * 1000 / FRAME_MS))
        )
        self._echo_tail_secs = echo_tail_secs
        self._correlation_threshold = correlation_threshold
        self._minimum_echo_rms = minimum_echo_rms
        self._low_noise_rms = low_noise_rms
        self._human_speech_rms = human_speech_rms
        self._human_speech_frames = human_speech_frames
        self._last_output_at = float("-inf")
        self._playback_active = False
        self._pending: _PendingInput | None = None
        self._non_echo_run = 0
        self._last_human_speech_at = float("-inf")
        self._input_frames = 0
        self._echo_suppressed_frames = 0
        self._noise_suppressed_frames = 0
        self._max_echo_correlation = 0.0

    @staticmethod
    def _samples(pcm: bytes) -> np.ndarray:
        if len(pcm) != FRAME_BYTES:
            raise ValueError(f"echo gate requires exactly {FRAME_BYTES} PCM bytes")
        return np.frombuffer(pcm, dtype="<i2").astype(np.float32)

    @staticmethod
    def _rms(samples: np.ndarray) -> float:
        return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0

    def set_playback_active(self, active: bool) -> None:
        self._playback_active = active

    def note_output_pcm(self, pcm: bytes) -> None:
        """Add exact phone-bound PCM to the rolling far-end reference."""

        if not pcm:
            return
        for offset in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[offset : offset + FRAME_BYTES]
            if len(frame) != FRAME_BYTES:
                continue
            # Eight kilohertz is sufficient for telephone echo matching and
            # halves the normalized-correlation cost.
            samples = self._samples(frame)[::2]
            self._reference_frames.append(samples)
        self._last_output_at = self._clock()

    def _echo_window_active(self, now: float) -> bool:
        return bool(
            self._reference_frames
            and (
                self._playback_active
                or now - self._last_output_at <= self._echo_tail_secs
            )
        )

    def _correlation(self, input_pcm: bytes) -> float:
        if len(input_pcm) != FRAME_BYTES * 2 or len(self._reference_frames) < 2:
            return 0.0
        incoming = np.frombuffer(input_pcm, dtype="<i2").astype(np.float32)[::2]
        if self._rms(incoming) < self._minimum_echo_rms:
            return 0.0
        reference = np.concatenate(tuple(self._reference_frames)).astype(np.float32, copy=False)
        if reference.size < incoming.size:
            return 0.0

        # First difference removes DC and makes matching robust to telephone
        # gain, mild filtering and polarity inversion.
        incoming = np.diff(incoming, prepend=incoming[0])
        reference = np.diff(reference, prepend=reference[0])
        incoming -= float(np.mean(incoming))
        incoming_norm = float(np.linalg.norm(incoming))
        if incoming_norm < 1.0:
            return 0.0
        correlation = np.correlate(reference, incoming, mode="valid")
        window_energy = np.convolve(
            reference * reference,
            np.ones(incoming.size, dtype=np.float32),
            mode="valid",
        )
        denominator = np.sqrt(np.maximum(window_energy, 1e-6)) * incoming_norm
        score = float(np.max(np.abs(correlation) / np.maximum(denominator, 1e-6)))
        return min(1.0, max(0.0, score))

    def process_input_frame(self, pcm: bytes) -> list[bytes]:
        """Return zero or more sanitized frames while preserving exact timing."""

        samples = self._samples(pcm)
        rms = self._rms(samples)
        now = self._clock()
        self._input_frames += 1

        if not self._echo_window_active(now):
            output: list[bytes] = []
            if self._pending is not None:
                output.append(self._release(self._pending, now))
                self._pending = None
            current = _PendingInput(pcm=pcm, rms=rms)
            output.append(self._release(current, now))
            return output

        current = _PendingInput(pcm=pcm, rms=rms)
        previous = self._pending
        self._pending = current
        if previous is None:
            return []

        score = self._correlation(previous.pcm + current.pcm)
        self._max_echo_correlation = max(self._max_echo_correlation, score)
        if score >= self._correlation_threshold:
            previous.force_suppress = True
            current.force_suppress = True
        elif previous.rms < self._low_noise_rms:
            previous.force_suppress = True
            self._noise_suppressed_frames += 1
        return [self._release(previous, now)]

    def _release(self, frame: _PendingInput, now: float) -> bytes:
        if frame.force_suppress:
            self._echo_suppressed_frames += 1
            self._non_echo_run = 0
            return b"\x00" * FRAME_BYTES
        if frame.rms >= self._human_speech_rms:
            self._non_echo_run += 1
            if self._non_echo_run >= self._human_speech_frames:
                self._last_human_speech_at = now
        else:
            self._non_echo_run = 0
        return frame.pcm

    def has_recent_human_speech(self, *, window_secs: float = 1.5) -> bool:
        return self._clock() - self._last_human_speech_at <= window_secs

    def discard_pending_input(self) -> None:
        self._pending = None
        self._non_echo_run = 0

    def reset(self) -> None:
        self._reference_frames.clear()
        self._pending = None
        self._playback_active = False
        self._last_output_at = float("-inf")
        self._non_echo_run = 0
        self._last_human_speech_at = float("-inf")

    def snapshot(self) -> dict[str, int | float]:
        return {
            "echo_gate_input_frames": self._input_frames,
            "echo_suppressed_frames": self._echo_suppressed_frames,
            "low_noise_suppressed_frames": self._noise_suppressed_frames,
            "max_echo_correlation": round(self._max_echo_correlation, 3),
        }
