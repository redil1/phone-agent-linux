"""Google GenAI Gemini 3.1 Flash TTS Service for PhoneAgent Telephony.

Streams low-latency 24kHz raw PCM from Google's multimodal audio engine,
resampling in real-time to 16kHz for seamless telephony transport. Supports
advanced prompt conditioning for natural English and French speech.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import numpy as np
import soxr
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts

from .runtime_config import DEFAULT_GOOGLE_TTS_SAMPLE_CONTEXT, DEFAULT_GOOGLE_TTS_SCENE

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore
    types = None  # type: ignore

logger = logging.getLogger("GoogleGenAITTS")


class StreamingPCMResampler:
    """Stateful PCM16 resampler that preserves continuity across API chunks."""

    def __init__(self, input_rate: int, output_rate: int) -> None:
        self.input_rate = input_rate
        self.output_rate = output_rate
        self._pending = b""
        self._stream = (
            soxr.ResampleStream(
                input_rate,
                output_rate,
                num_channels=1,
                dtype="int16",
                quality="MQ",
            )
            if input_rate != output_rate
            else None
        )

    def push(self, audio: bytes, *, final: bool = False) -> bytes:
        data = self._pending + audio
        aligned = len(data) - (len(data) % 2)
        self._pending = data[aligned:]
        samples = np.frombuffer(data[:aligned], dtype=np.int16)
        if self._stream is None:
            return samples.tobytes()
        converted = self._stream.resample_chunk(samples, last=final)
        return converted.astype(np.int16, copy=False).tobytes()


@dataclass
class GoogleGenAITTSSettings(TTSSettings):
    """Runtime settings for Google GenAI TTS."""

    model: str = "gemini-3.1-flash-tts-preview"
    voice: str = "Aoede"
    language: str = "en-US"


class GoogleGenAITTSService(TTSService):
    """Pipecat TTS adapter for Google's Gemini 3.1 Flash TTS Preview engine."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "gemini-3.1-flash-tts-preview",
        voice: str = "Aoede",
        language: str = "en-US",
        scene: str = DEFAULT_GOOGLE_TTS_SCENE,
        sample_context: str = DEFAULT_GOOGLE_TTS_SAMPLE_CONTEXT,
        sample_rate: int = 16_000,
        first_audio_timeout_secs: float = 5.0,
        chunk_timeout_secs: float = 5.0,
        total_timeout_secs: float = 30.0,
        max_attempts: int = 2,
        fallback_enabled: bool = True,
        fallback_voice: str = "en-US-AndrewMultilingualNeural",
        fallback_service: Any = None,
        model_fallback_enabled: bool = True,
        fallback_model: str = "gemini-2.5-flash-preview-tts",
        primary_quota_cooldown_secs: float = 60.0,
        fallback_model_timeout_secs: float = 12.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            sample_rate=sample_rate,
            settings=GoogleGenAITTSSettings(
                model=model,
                voice=voice,
                language=language,
            ),
            **kwargs,
        )
        self._api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self._model = model
        self._voice = voice
        self._language = language
        self._scene = scene.strip()
        self._sample_context = sample_context.strip()
        self._output_sample_rate = sample_rate
        self._first_audio_timeout_secs = first_audio_timeout_secs
        self._chunk_timeout_secs = chunk_timeout_secs
        self._total_timeout_secs = total_timeout_secs
        self._max_attempts = max(1, max_attempts)
        self._fallback_enabled = fallback_enabled
        self._fallback_voice = fallback_voice
        self._fallback_service = fallback_service
        self._fallback_reason = ""
        self._model_fallback_enabled = model_fallback_enabled
        self._fallback_model = fallback_model
        self._primary_quota_cooldown_secs = max(0.0, primary_quota_cooldown_secs)
        self._fallback_model_timeout_secs = max(1.0, fallback_model_timeout_secs)
        self._primary_backoff_until = 0.0
        self._client: Any = None
        self._ensure_client()

    def _ensure_client(self) -> None:
        if self._client is None:
            if genai is None:
                raise RuntimeError("google-genai package is required for GoogleGenAITTSService")
            self._client = genai.Client(api_key=self._api_key)

    async def _close_stream(self, stream: Any) -> None:
        close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=2.0)
        except Exception as exc:
            logger.debug("Could not close Google TTS stream cleanly: %s", exc)

    async def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        close = getattr(client.aio, "aclose", None)
        if close is None:
            return
        try:
            await asyncio.wait_for(close(), timeout=2.0)
        except Exception as exc:
            logger.debug("Could not close Google TTS client cleanly: %s", exc)

    async def _recreate_client(self) -> None:
        await self._close_client()
        self._ensure_client()

    def _ensure_fallback_service(self) -> Any:
        if self._fallback_service is None:
            from .edge_tts_service import EdgeTTSService

            self._fallback_service = EdgeTTSService(
                sample_rate=self._output_sample_rate,
                voice=self._fallback_voice,
                phrase_aggregation=False,
            )
        return self._fallback_service

    @staticmethod
    def _provider_error_details(exc: Exception) -> tuple[str, bool]:
        raw = str(exc)
        normalized = raw.lower()
        quota_exhausted = "429" in normalized and (
            "quota" in normalized or "resource_exhausted" in normalized
        )
        if quota_exhausted:
            return "Google TTS quota exhausted (HTTP 429)", False
        if "permission_denied" in normalized or "api key not valid" in normalized:
            return "Google TTS credentials were rejected", False
        return raw or type(exc).__name__, True

    async def _run_fallback(
        self,
        text: str,
        context_id: str,
    ) -> AsyncGenerator[Frame, None]:
        service = self._ensure_fallback_service()
        logger.warning(
            "Using Edge TTS fallback voice=%s reason=%s",
            self._fallback_voice,
            self._fallback_reason,
        )
        async for frame in service.run_tts(text, context_id):
            yield frame

    @staticmethod
    def _response_audio(response: Any) -> bytes:
        chunks: list[bytes] = []
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline_data = getattr(part, "inline_data", None)
                data = getattr(inline_data, "data", None)
                if data:
                    chunks.append(data)
        return b"".join(chunks)

    async def _run_nonstream_model(
        self,
        *,
        model: str,
        prompt: str,
        config: Any,
        context_id: str,
    ) -> AsyncGenerator[Frame, None]:
        """Generate audio for Gemini TTS models without streaming support."""
        response = await asyncio.wait_for(
            self._client.aio.models.generate_content(
                model=model,
                contents=[
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=prompt)],
                    )
                ],
                config=config,
            ),
            timeout=self._fallback_model_timeout_secs,
        )
        raw_audio = self._response_audio(response)
        if not raw_audio:
            raise RuntimeError("Gemini non-streaming model completed without audio")

        resampler = StreamingPCMResampler(24_000, self._output_sample_rate)
        pcm = resampler.push(raw_audio) + resampler.push(b"", final=True)
        if not pcm:
            raise RuntimeError("Gemini non-streaming model returned empty PCM audio")

        # Small continuous frames preserve telephony pacing without introducing
        # discontinuities into the already-resampled PCM waveform.
        frame_bytes = max(2, (self._output_sample_rate * 2 * 40) // 1000)
        for offset in range(0, len(pcm), frame_bytes):
            yield TTSAudioRawFrame(
                audio=pcm[offset : offset + frame_bytes],
                sample_rate=self._output_sample_rate,
                num_channels=1,
                context_id=context_id,
            )

    def can_generate_metrics(self) -> bool:
        return True

    def _format_prompt(self, text: str) -> str:
        """Build a directed performance prompt with an isolated verbatim transcript."""
        sections = [
            "Synthesize speech for the transcript below. Read only the transcript aloud; "
            "never speak these instructions, section headings, or delimiters. Preserve the "
            "transcript's language, names, numbers, wording, and meaning exactly. Do not "
            "translate, add, omit, repeat, or answer the transcript.",
        ]
        if self._scene:
            sections.append(f"## THE SCENE\n{self._scene}")
        if self._sample_context:
            sections.append(f"### SAMPLE CONTEXT\n{self._sample_context}")
        sections.append(
            "### DIRECTOR'S NOTES\n"
            "Use a warm, natural, professional telephone voice. Keep the delivery "
            "conversational and responsive to the meaning of this exact turn."
        )
        sections.append(f"#### TRANSCRIPT TO SPEAK VERBATIM\n{text.strip()}")
        return "\n\n".join(sections)

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Stream raw audio frames from Google Gemini 3.1 Flash TTS."""
        if not text.strip():
            return

        self._ensure_client()
        prompt = self._format_prompt(text)

        config = types.GenerateContentConfig(
            temperature=1.0,
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                )
            ),
        )

        emitted_audio = False
        last_error = "Google GenAI TTS did not return audio"
        try:
            await self.start_tts_usage_metrics(text)
            primary_uses_nonstreaming = self._model == "gemini-2.5-flash-preview-tts"
            if primary_uses_nonstreaming:
                try:
                    async for frame in self._run_nonstream_model(
                        model=self._model,
                        prompt=prompt,
                        config=config,
                        context_id=context_id,
                    ):
                        if isinstance(frame, TTSAudioRawFrame):
                            if not emitted_audio:
                                await self.stop_ttfb_metrics()
                            emitted_audio = True
                        yield frame
                    if emitted_audio:
                        self._fallback_reason = ""
                        return
                except Exception as exc:
                    last_error, _retryable = self._provider_error_details(exc)
                    self._fallback_reason = last_error
                    logger.warning(
                        "Selected Gemini TTS model failed model=%s error=%s",
                        self._model,
                        last_error,
                    )

            primary_in_backoff = (
                self._model_fallback_enabled and time.monotonic() < self._primary_backoff_until
            )
            if primary_in_backoff:
                logger.info(
                    "Skipping quota-limited Gemini TTS primary; using Gemini model fallback"
                )

            attempts = (
                ()
                if primary_in_backoff or primary_uses_nonstreaming
                else range(1, self._max_attempts + 1)
            )
            for attempt in attempts:
                stream: Any = None
                attempt_emitted_audio = False
                resampler = StreamingPCMResampler(24_000, self._output_sample_rate)
                request_started = time.monotonic()
                try:
                    try:
                        stream = await asyncio.wait_for(
                            self._client.aio.models.generate_content_stream(
                                model=self._model,
                                contents=[
                                    types.Content(
                                        role="user",
                                        parts=[types.Part.from_text(text=prompt)],
                                    )
                                ],
                                config=config,
                            ),
                            timeout=self._first_audio_timeout_secs,
                        )
                    except TimeoutError as exc:
                        raise TimeoutError("provider request deadline exceeded") from exc
                    iterator = stream.__aiter__()
                    while True:
                        elapsed = time.monotonic() - request_started
                        remaining_total = self._total_timeout_secs - elapsed
                        if remaining_total <= 0:
                            raise TimeoutError("total synthesis deadline exceeded")
                        timeout = min(self._chunk_timeout_secs, remaining_total)
                        if not attempt_emitted_audio:
                            remaining_first_audio = self._first_audio_timeout_secs - elapsed
                            if remaining_first_audio <= 0:
                                raise TimeoutError("first-audio deadline exceeded")
                            timeout = min(timeout, remaining_first_audio)
                        try:
                            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            reason = (
                                "first-audio deadline exceeded"
                                if not attempt_emitted_audio
                                else "audio stream inactivity deadline exceeded"
                            )
                            raise TimeoutError(reason) from exc
                        for part in chunk.parts or []:
                            data = part.inline_data.data if part.inline_data else None
                            if not data:
                                continue
                            pcm = resampler.push(data)
                            if not pcm:
                                continue
                            if not emitted_audio:
                                await self.stop_ttfb_metrics()
                            emitted_audio = True
                            attempt_emitted_audio = True
                            yield TTSAudioRawFrame(
                                audio=pcm,
                                sample_rate=self._output_sample_rate,
                                num_channels=1,
                                context_id=context_id,
                            )
                    tail = resampler.push(b"", final=True)
                    if tail:
                        if not emitted_audio:
                            await self.stop_ttfb_metrics()
                        emitted_audio = True
                        attempt_emitted_audio = True
                        yield TTSAudioRawFrame(
                            audio=tail,
                            sample_rate=self._output_sample_rate,
                            num_channels=1,
                            context_id=context_id,
                        )
                    if not attempt_emitted_audio:
                        raise RuntimeError("provider stream completed without audio")
                    self._primary_backoff_until = 0.0
                    self._fallback_reason = ""
                    return
                except Exception as exc:
                    last_error, retryable = self._provider_error_details(exc)
                    self._fallback_reason = last_error
                    if (
                        last_error == "Google TTS quota exhausted (HTTP 429)"
                        and not attempt_emitted_audio
                    ):
                        self._primary_backoff_until = (
                            time.monotonic() + self._primary_quota_cooldown_secs
                        )
                    safe_to_retry = (
                        retryable and not attempt_emitted_audio and attempt < self._max_attempts
                    )
                    logger.warning(
                        "Google TTS attempt failed attempt=%d/%d retry=%s error=%s",
                        attempt,
                        self._max_attempts,
                        safe_to_retry,
                        last_error,
                    )
                    if not safe_to_retry:
                        break
                finally:
                    if stream is not None:
                        await self._close_stream(stream)

                await self._recreate_client()

            if (
                not emitted_audio
                and self._model_fallback_enabled
                and self._fallback_model != self._model
            ):
                gemini_fallback_error = ""
                try:
                    logger.warning(
                        "Using Gemini TTS model fallback primary=%s fallback=%s reason=%s",
                        self._model,
                        self._fallback_model,
                        self._fallback_reason,
                    )
                    async for frame in self._run_nonstream_model(
                        model=self._fallback_model,
                        prompt=prompt,
                        config=config,
                        context_id=context_id,
                    ):
                        if isinstance(frame, TTSAudioRawFrame):
                            if not emitted_audio:
                                await self.stop_ttfb_metrics()
                            emitted_audio = True
                        yield frame
                    if emitted_audio:
                        return
                except Exception as exc:
                    gemini_fallback_error = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Gemini TTS model fallback failed model=%s error=%s",
                        self._fallback_model,
                        gemini_fallback_error,
                    )
                last_error = (
                    f"{last_error}; Gemini model fallback failed: "
                    f"{gemini_fallback_error or 'no audio returned'}"
                )

            if not emitted_audio and self._fallback_enabled:
                self._fallback_reason = last_error
                fallback_emitted_audio = False
                fallback_error = ""
                try:
                    async for frame in self._run_fallback(text, context_id):
                        if isinstance(frame, TTSAudioRawFrame):
                            fallback_emitted_audio = True
                            yield frame
                        elif isinstance(frame, ErrorFrame):
                            fallback_error = frame.error
                    if fallback_emitted_audio and not fallback_error:
                        return
                except Exception as exc:
                    fallback_error = f"{type(exc).__name__}: {exc}"
                last_error = (
                    f"{last_error}; Edge TTS fallback failed: "
                    f"{fallback_error or 'no audio returned'}"
                )

            # Reset a timed-out or failed session so the next caller turn gets a
            # clean provider connection even when this turn cannot be replayed.
            await self._recreate_client()
            yield ErrorFrame(error=f"Google GenAI TTS failed: {last_error}")
            if self.audio_context_available(context_id):
                await self.remove_audio_context(context_id)
        finally:
            await self.stop_ttfb_metrics()

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._close_client()
        if self._fallback_service is not None:
            await self._fallback_service.cleanup()
            self._fallback_service = None
