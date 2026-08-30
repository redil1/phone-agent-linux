"""Cancellable Edge neural TTS adapter with continuous MP3 decoding.

``edge-tts`` streams audio bytes for a complete input phrase, but the service
only exposes MP3/WebM rather than telephone-ready PCM.  This adapter keeps one
MP3 decoder and one resampler alive for the whole phrase so arbitrary network
chunk boundaries never become audible gaps in the 16 kHz phone stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import edge_tts
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import NOT_GIVEN, TTSSettings, _NotGiven, assert_given
from pipecat.services.tts_service import TextAggregationMode, TTSService
from pipecat.utils.text.base_text_aggregator import (
    Aggregation,
    AggregationType,
    BaseTextAggregator,
)
from pipecat.utils.tracing.service_decorators import traced_tts

_SPEAKABLE_RE = re.compile(r"[\w\d]", re.UNICODE)
logger = logging.getLogger("PhoneAgentEdgeTTS")


def split_edge_phrases(text: str, *, min_chars: int, max_chars: int) -> list[str]:
    """Apply the live phrase aggregator's exact deterministic boundaries."""

    phrases: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        stripped = buffer.strip()
        if not stripped:
            continue
        terminal = char in ".!?\u3002\uff01\uff1f"
        soft_boundary = char in ",;:\uff0c\uff1b\uff1a" and len(stripped) >= min_chars
        length_boundary = len(stripped) >= max_chars and (
            char.isspace() or len(stripped) >= max_chars + 16
        )
        if terminal or soft_boundary or length_boundary:
            phrases.append(stripped)
            buffer = ""
    if buffer.strip():
        phrases.append(buffer.strip())
    return phrases


class EdgeCommunicator(Protocol):
    """Narrow interface used to make the network boundary testable."""

    def stream(self) -> AsyncGenerator[dict[str, Any], None]: ...


CommunicatorFactory = Callable[..., EdgeCommunicator]


class PhraseTextAggregator(BaseTextAggregator):
    """Release natural, bounded phrases from a streaming LLM response."""

    def __init__(self, *, min_chars: int = 12, max_chars: int = 60) -> None:
        if not 1 <= min_chars <= max_chars:
            raise ValueError("phrase min_chars must be positive and <= max_chars")
        super().__init__(aggregation_type=AggregationType.SENTENCE)
        self._min_chars = min_chars
        self._max_chars = max_chars
        self._buffer = ""

    @property
    def text(self) -> Aggregation:
        return Aggregation(text=self._buffer.strip(), type=AggregationType.SENTENCE)

    async def aggregate(self, text: str) -> AsyncGenerator[Aggregation, None]:
        for char in text:
            self._buffer += char
            stripped = self._buffer.strip()
            if not stripped:
                continue
            terminal = char in ".!?\u3002\uff01\uff1f"
            soft_boundary = char in ",;:\uff0c\uff1b\uff1a" and len(stripped) >= self._min_chars
            length_boundary = len(stripped) >= self._max_chars and (
                char.isspace() or len(stripped) >= self._max_chars + 16
            )
            if terminal or soft_boundary or length_boundary:
                result = stripped
                self._buffer = ""
                yield Aggregation(text=result, type=AggregationType.SENTENCE)

    async def flush(self) -> Aggregation | None:
        if not self._buffer.strip():
            await self.reset()
            return None
        result = self.text
        await self.reset()
        return result

    async def handle_interruption(self) -> None:
        await self.reset()

    async def reset(self) -> None:
        self._buffer = ""


@dataclass
class EdgeTTSSettings(TTSSettings):
    """Runtime settings accepted by the Edge neural speech endpoint."""

    rate: str | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    volume: str | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    pitch: str | _NotGiven = field(default_factory=lambda: NOT_GIVEN)


@dataclass
class _PrefetchedPCMStream:
    chunks: list[bytes] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    done: bool = False

    def append(self, pcm: bytes) -> None:
        self.chunks.append(pcm)
        self.changed.set()

    def finish(self) -> None:
        self.done = True
        self.changed.set()

    async def read(self) -> AsyncGenerator[bytes, None]:
        index = 0
        while True:
            while index < len(self.chunks):
                yield self.chunks[index]
                index += 1
            if self.done:
                return
            self.changed.clear()
            if index < len(self.chunks) or self.done:
                continue
            await self.changed.wait()


class FFmpegMP3StreamDecoder:
    """Decode one streamed MP3 phrase through a cancellable FFmpeg process."""

    def __init__(self, sample_rate: int, *, binary: str = "ffmpeg") -> None:
        self.sample_rate = sample_rate
        self.binary = binary
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(
            self.binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "mp3",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(self.sample_rate),
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def write(self, data: bytes) -> None:
        process = self._require_process()
        if process.stdin is None or process.stdin.is_closing():
            raise RuntimeError("FFmpeg MP3 input is closed")
        process.stdin.write(data)
        await process.stdin.drain()

    async def close_input(self) -> None:
        process = self._require_process()
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            await process.stdin.wait_closed()

    async def read(self, chunk_bytes: int = 4096) -> AsyncGenerator[bytes, None]:
        process = self._require_process()
        if process.stdout is None:
            raise RuntimeError("FFmpeg MP3 output is unavailable")
        pending = b""
        while chunk := await process.stdout.read(chunk_bytes):
            pending += chunk
            usable = len(pending) & ~1
            if usable:
                yield pending[:usable]
                pending = pending[usable:]
        if pending:
            raise RuntimeError("FFmpeg returned a partial PCM16 sample")

    async def wait(self) -> None:
        process = self._require_process()
        return_code = await process.wait()
        if return_code:
            error = b""
            if process.stderr is not None:
                error = await process.stderr.read(8192)
            detail = error.decode(errors="replace").strip()[:2000]
            raise RuntimeError(f"FFmpeg MP3 decoder exited {return_code}: {detail}")

    async def cancel(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RuntimeError("FFmpeg MP3 decoder is not started")
        return self._process


class EdgeTTSService(TTSService):
    """Pipecat TTS service for Microsoft's online Edge neural voices."""

    Settings = EdgeTTSSettings
    _settings: EdgeTTSSettings

    def __init__(
        self,
        *,
        voice: str = "en-US-EmmaMultilingualNeural",
        rate: str = "+0%",
        volume: str = "+0%",
        pitch: str = "+0Hz",
        sample_rate: int = 16_000,
        connect_timeout_secs: int = 5,
        receive_timeout_secs: int = 20,
        live_attempts: int = 3,
        ffmpeg_binary: str = "ffmpeg",
        text_aggregation_mode: TextAggregationMode = TextAggregationMode.SENTENCE,
        phrase_aggregation: bool = True,
        phrase_min_chars: int = 24,
        phrase_max_chars: int = 72,
        reflex_cache_dir: Path | None = None,
        communicator_factory: CommunicatorFactory = edge_tts.Communicate,
        **kwargs: Any,
    ) -> None:
        settings = self.Settings(
            model="edge-online-neural",
            voice=voice,
            language=None,
            rate=rate,
            volume=volume,
            pitch=pitch,
        )
        super().__init__(
            sample_rate=sample_rate,
            text_aggregation_mode=text_aggregation_mode,
            push_start_frame=True,
            push_stop_frames=True,
            settings=settings,
            **kwargs,
        )
        self._connect_timeout_secs = connect_timeout_secs
        self._receive_timeout_secs = receive_timeout_secs
        if not 1 <= live_attempts <= 3:
            raise ValueError("live_attempts must be between 1 and 3")
        self._live_attempts = live_attempts
        self._ffmpeg_binary = ffmpeg_binary
        self._target_sample_rate = sample_rate
        self._communicator_factory = communicator_factory
        self._phrase_aggregation = phrase_aggregation
        self._phrase_min_chars = phrase_min_chars
        self._phrase_max_chars = phrase_max_chars
        self._prefetch_cache: dict[str, bytes] = {}
        self._prefetch_phrase_tasks: dict[str, asyncio.Task[bytes]] = {}
        self._prefetch_streams: dict[str, _PrefetchedPCMStream] = {}
        self._reflex_cache_dir = reflex_cache_dir or (
            Path.home() / ".cache" / "phone-agent" / "reflexes"
        )
        self._reflex_cache: dict[str, bytes] = {}
        self._reflex_tasks: dict[str, asyncio.Task[bytes]] = {}
        if phrase_aggregation:
            self._text_aggregator = PhraseTextAggregator(
                min_chars=phrase_min_chars,
                max_chars=phrase_max_chars,
            )

    def clear_prefetch(self) -> None:
        for task in self._prefetch_phrase_tasks.values():
            if not task.done():
                task.cancel()
        for stream in self._prefetch_streams.values():
            stream.finish()
        self._prefetch_phrase_tasks.clear()
        self._prefetch_streams.clear()
        self._prefetch_cache.clear()

    async def prefetch_text(self, text: str) -> None:
        """Synthesize one speculative response without emitting audio."""

        phrases = (
            split_edge_phrases(
                text,
                min_chars=self._phrase_min_chars,
                max_chars=self._phrase_max_chars,
            )
            if self._phrase_aggregation
            else [text.strip()]
        )
        tasks: list[asyncio.Task[bytes]] = []
        for phrase in phrases:
            if not phrase or not _SPEAKABLE_RE.search(phrase) or phrase in self._prefetch_cache:
                continue
            task = self._prefetch_phrase_tasks.get(phrase)
            if task is None:
                stream = _PrefetchedPCMStream()
                self._prefetch_streams[phrase] = stream
                task = asyncio.create_task(
                    self._synthesize_prefetch_pcm(phrase, stream),
                    name="phoneagent-speculative-edge-tts",
                )
                self._prefetch_phrase_tasks[phrase] = task
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks)

    async def _synthesize_prefetch_pcm(
        self,
        phrase: str,
        stream: _PrefetchedPCMStream,
    ) -> bytes:
        try:
            pcm = await self._decode_edge_pcm(phrase, stream)
            if pcm:
                self._prefetch_cache[phrase] = pcm
            return pcm
        finally:
            stream.finish()
            self._prefetch_phrase_tasks.pop(phrase, None)

    async def _decode_edge_pcm(
        self,
        phrase: str,
        stream: _PrefetchedPCMStream | None = None,
    ) -> bytes:
        voice = assert_given(self._settings.voice)
        rate = assert_given(self._settings.rate)
        volume = assert_given(self._settings.volume)
        pitch = assert_given(self._settings.pitch)
        decoder = FFmpegMP3StreamDecoder(self._target_sample_rate, binary=self._ffmpeg_binary)
        writer_task: asyncio.Task[None] | None = None
        try:
            communicator = self._communicator_factory(
                text=phrase,
                voice=voice,
                rate=rate,
                volume=volume,
                pitch=pitch,
                boundary="SentenceBoundary",
                connect_timeout=self._connect_timeout_secs,
                receive_timeout=self._receive_timeout_secs,
            )
            await decoder.start()

            async def write_mp3_stream() -> None:
                try:
                    async for chunk in communicator.stream():
                        if chunk.get("type") == "audio" and chunk.get("data"):
                            await decoder.write(chunk["data"])
                finally:
                    await decoder.close_input()

            writer_task = asyncio.create_task(write_mp3_stream())
            chunks: list[bytes] = []
            async for chunk in decoder.read():
                chunks.append(chunk)
                if stream is not None:
                    stream.append(chunk)
            pcm = b"".join(chunks)
            await writer_task
            await decoder.wait()
            return pcm
        except asyncio.CancelledError:
            if writer_task is not None:
                writer_task.cancel()
                await asyncio.gather(writer_task, return_exceptions=True)
            await decoder.cancel()
            raise
        except Exception:
            if writer_task is not None and not writer_task.done():
                writer_task.cancel()
                await asyncio.gather(writer_task, return_exceptions=True)
            await decoder.cancel()
            logger.debug("Edge TTS background synthesis failed", exc_info=True)
            return b""

    async def synthesize_pcm(self, text: str) -> bytes:
        """Render telephone-ready PCM for another provider's safe fallback path."""

        phrase = text.strip()
        if not phrase or not _SPEAKABLE_RE.search(phrase):
            return b""
        return await self._decode_edge_pcm(phrase)

    def has_ready_speculative_audio(self) -> bool:
        """Return true only when substantive speculative PCM can start now."""

        if any(self._prefetch_cache.values()):
            return True
        return any(stream.chunks for stream in self._prefetch_streams.values())

    def get_reflex_pcm(self, phrase: str) -> bytes | None:
        """Read a voice/settings-specific persistent reflex without network I/O."""

        cached = self._reflex_cache.get(phrase)
        if cached:
            return cached
        path = self._reflex_path(phrase)
        try:
            pcm = path.read_bytes()
        except OSError:
            return None
        maximum = self._target_sample_rate * 2 * 5
        if not pcm or len(pcm) % 2 or len(pcm) > maximum:
            return None
        self._reflex_cache[phrase] = pcm
        return pcm

    async def warm_reflexes(self, phrases: tuple[str, ...]) -> None:
        """Populate reusable Andrew PCM; failures leave the normal path untouched."""

        tasks: list[asyncio.Task[bytes]] = []
        for phrase in phrases:
            phrase = phrase.strip()
            if not phrase or self.get_reflex_pcm(phrase):
                continue
            task = self._reflex_tasks.get(phrase)
            if task is None:
                task = asyncio.create_task(
                    self._warm_reflex_phrase(phrase),
                    name="phoneagent-edge-reflex-warmup",
                )
                self._reflex_tasks[phrase] = task
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks)

    async def _warm_reflex_phrase(self, phrase: str) -> bytes:
        try:
            pcm = await self._decode_edge_pcm(phrase)
            if not pcm:
                return b""
            pcm = self._trim_reflex_pcm(pcm)
            if not pcm:
                return b""
            self._reflex_cache[phrase] = pcm
            path = self._reflex_path(phrase)
            await asyncio.to_thread(self._write_reflex_atomic, path, pcm)
            return pcm
        finally:
            self._reflex_tasks.pop(phrase, None)

    def _reflex_path(self, phrase: str) -> Path:
        settings_key = "\0".join(
            (
                "reflex-v2-trimmed",
                str(assert_given(self._settings.voice)),
                str(assert_given(self._settings.rate)),
                str(assert_given(self._settings.volume)),
                str(assert_given(self._settings.pitch)),
                str(self._target_sample_rate),
                phrase,
            )
        )
        digest = hashlib.sha256(settings_key.encode("utf-8")).hexdigest()[:24]
        return self._reflex_cache_dir / f"{digest}.pcm"

    def _trim_reflex_pcm(self, pcm: bytes) -> bytes:
        """Remove provider padding while preserving a natural speech tail."""

        if len(pcm) < 2 or len(pcm) % 2:
            return b""
        samples = memoryview(pcm).cast("h")
        threshold = 80
        first = next((index for index, value in enumerate(samples) if abs(value) > threshold), None)
        if first is None:
            return b""
        last = next(
            index
            for index in range(len(samples) - 1, first - 1, -1)
            if abs(samples[index]) > threshold
        )
        leading_padding = int(self._target_sample_rate * 0.04)
        trailing_padding = int(self._target_sample_rate * 0.10)
        start = max(0, first - leading_padding)
        end = min(len(samples), last + trailing_padding + 1)
        return pcm[start * 2 : end * 2]

    @staticmethod
    def _write_reflex_atomic(path: Path, pcm: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(pcm)
        temporary.replace(path)

    async def cleanup(self) -> None:
        self.clear_prefetch()
        tasks = tuple(self._reflex_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reflex_tasks.clear()
        await super().cleanup()

    def can_generate_metrics(self) -> bool:
        return True

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        phrase = text.strip()
        if not phrase or not _SPEAKABLE_RE.search(phrase):
            return

        prefetched = self._prefetch_cache.get(phrase)
        prefetched_stream = self._prefetch_streams.get(phrase)
        if prefetched:
            await self.start_tts_usage_metrics(phrase)
            await self.stop_ttfb_metrics()
            logger.info("speculative Edge TTS cache hit chars=%d", len(phrase))
            for offset in range(0, len(prefetched), 4096):
                yield TTSAudioRawFrame(
                    audio=prefetched[offset : offset + 4096],
                    sample_rate=self._target_sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )
            return
        if prefetched_stream is not None:
            await self.start_tts_usage_metrics(phrase)
            emitted_audio = False
            async for pcm in prefetched_stream.read():
                if not emitted_audio:
                    await self.stop_ttfb_metrics()
                    emitted_audio = True
                    logger.info("attached to speculative Edge TTS stream chars=%d", len(phrase))
                yield TTSAudioRawFrame(
                    audio=pcm,
                    sample_rate=self._target_sample_rate,
                    num_channels=1,
                    context_id=context_id,
                )
            if emitted_audio:
                return

        voice = assert_given(self._settings.voice)
        rate = assert_given(self._settings.rate)
        volume = assert_given(self._settings.volume)
        pitch = assert_given(self._settings.pitch)
        if not all(isinstance(value, str) and value for value in (voice, rate, volume, pitch)):
            yield ErrorFrame(error="Edge TTS voice and prosody settings must be configured")
            return

        await self.start_tts_usage_metrics(phrase)
        emitted_audio = False
        last_error = "Edge TTS completed without decodable audio"
        try:
            for attempt in range(1, self._live_attempts + 1):
                decoder = FFmpegMP3StreamDecoder(
                    self._target_sample_rate,
                    binary=self._ffmpeg_binary,
                )
                writer_task: asyncio.Task[None] | None = None
                attempt_emitted_audio = False
                try:
                    communicator = self._communicator_factory(
                        text=phrase,
                        voice=voice,
                        rate=rate,
                        volume=volume,
                        pitch=pitch,
                        boundary="SentenceBoundary",
                        connect_timeout=self._connect_timeout_secs,
                        receive_timeout=self._receive_timeout_secs,
                    )
                    await decoder.start()

                    async def write_mp3_stream(
                        active_communicator: EdgeCommunicator = communicator,
                        active_decoder: FFmpegMP3StreamDecoder = decoder,
                    ) -> None:
                        try:
                            async for chunk in active_communicator.stream():
                                if chunk.get("type") != "audio":
                                    continue
                                data = chunk.get("data")
                                if not isinstance(data, bytes):
                                    raise TypeError("Edge TTS returned a non-bytes audio chunk")
                                if data:
                                    await active_decoder.write(data)
                        finally:
                            await active_decoder.close_input()

                    writer_task = asyncio.create_task(write_mp3_stream())
                    async for pcm in decoder.read():
                        if not emitted_audio:
                            await self.stop_ttfb_metrics()
                            emitted_audio = True
                        attempt_emitted_audio = True
                        yield TTSAudioRawFrame(
                            audio=pcm,
                            sample_rate=self._target_sample_rate,
                            num_channels=1,
                            context_id=context_id,
                        )
                    await writer_task
                    await decoder.wait()
                    if attempt_emitted_audio:
                        return
                    last_error = "Edge TTS completed without decodable audio"
                except asyncio.CancelledError:
                    if writer_task is not None:
                        writer_task.cancel()
                        await asyncio.gather(writer_task, return_exceptions=True)
                    await decoder.cancel()
                    raise
                except Exception as exc:
                    if writer_task is not None and not writer_task.done():
                        writer_task.cancel()
                        await asyncio.gather(writer_task, return_exceptions=True)
                    await decoder.cancel()
                    last_error = f"Edge TTS failed: {type(exc).__name__}: {exc}"
                    if attempt_emitted_audio:
                        logger.error(
                            "Edge TTS stream failed after audio started attempt=%d/%d error=%s",
                            attempt,
                            self._live_attempts,
                            last_error,
                        )
                        yield ErrorFrame(error=last_error)
                        return

                if attempt < self._live_attempts:
                    logger.warning(
                        "Edge TTS live attempt failed attempt=%d/%d chars=%d error=%s; retrying",
                        attempt,
                        self._live_attempts,
                        len(phrase),
                        last_error,
                    )
                    await asyncio.sleep(0.08 if attempt == 1 else 0.18)

            logger.error(
                "Edge TTS live retries exhausted attempts=%d chars=%d error=%s",
                self._live_attempts,
                len(phrase),
                last_error,
            )
            yield ErrorFrame(error=last_error)
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop_ttfb_metrics()
