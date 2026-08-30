"""Tool-disabled Pipecat adapter for a locally authenticated Gemini CLI.

This is deliberately separate from Antigravity.  It uses the supported
``gemini`` command and its own Google OAuth login; it never reads, copies, or
replays Antigravity credentials or calls Antigravity's private localhost API.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    StartFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings


class GeminiCliError(RuntimeError):
    """The local Gemini CLI could not produce a completion."""


def resolve_gemini_binary(configured: str | None = None) -> str:
    """Resolve the CLI executable without inspecting any credential store."""

    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise GeminiCliError(f"configured Gemini CLI does not exist: {path}")
        return str(path)
    discovered = shutil.which("gemini")
    if discovered:
        return discovered
    raise GeminiCliError("Gemini CLI was not found on PATH")


def _safe_error_summary(raw: str) -> str:
    """Return a short diagnostic while removing common credential/PII shapes."""

    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<redacted-email>", raw)
    text = re.sub(
        r"(?i)(token|secret|authorization|credential)(\s*[:=]\s*)\S+",
        r"\1\2<redacted>",
        text,
    )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    useful = [
        line
        for line in lines
        if not line.startswith("at ")
        and not line.startswith("file://")
        and "node:internal" not in line
    ]
    return (
        " | ".join((useful or lines)[:6])[:2000]
        or "Gemini CLI exited without an error message"
    )


class GeminiCliClient:
    """Run one tool-disabled, stateless Gemini CLI process per turn."""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = resolve_gemini_binary(binary)
        self._workspace = tempfile.TemporaryDirectory(prefix="phone-agent-gemini-")
        self._process: asyncio.subprocess.Process | None = None
        self._process_lock = asyncio.Lock()
        settings_dir = Path(self._workspace.name, ".gemini")
        settings_dir.mkdir(mode=0o700)
        Path(settings_dir, "settings.json").write_text(
            json.dumps(
                {
                    "coreTools": [],
                    "mcpServers": {},
                    "contextFileName": ".phone-agent-no-context",
                    "telemetry": {"enabled": False},
                    "usageStatisticsEnabled": False,
                    "autoConfigureMaxOldSpaceSize": False,
                }
            ),
            encoding="utf-8",
        )

    async def stream_completion(
        self,
        *,
        model: str,
        prompt: str,
        timeout_secs: float,
    ) -> AsyncIterator[str]:
        if not prompt.strip():
            raise GeminiCliError("Gemini CLI prompt is empty")

        async with self._process_lock:
            env = os.environ.copy()
            env.update(
                {
                    "GEMINI_CLI_NO_RELAUNCH": "true",
                    "NO_COLOR": "1",
                    "TERM": "dumb",
                }
            )
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "--model",
                model,
                "--telemetry",
                "false",
                cwd=self._workspace.name,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._process = process
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            stderr_task = asyncio.create_task(self._capture_stderr(process.stderr))
            decoder = codecs.getincrementaldecoder("utf-8")()
            try:
                async with asyncio.timeout(timeout_secs):
                    while chunk := await process.stdout.read(512):
                        text = decoder.decode(chunk)
                        if text:
                            yield text
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        yield tail
                    return_code = await process.wait()
                    stderr = (await stderr_task).decode(errors="replace")
                    if return_code != 0:
                        raise GeminiCliError(_safe_error_summary(stderr))
            except (asyncio.CancelledError, TimeoutError):
                await asyncio.shield(self._terminate_process(process))
                raise
            finally:
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                if self._process is process:
                    self._process = None

    async def close(self) -> None:
        process = self._process
        if process is not None:
            await self._terminate_process(process)
            self._process = None
        self._workspace.cleanup()

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    async def _capture_stderr(reader: asyncio.StreamReader) -> bytes:
        """Drain stderr without unbounded memory while preserving useful context."""

        head = bytearray()
        tail = bytearray()
        while chunk := await reader.read(4096):
            remaining_head = 8192 - len(head)
            if remaining_head > 0:
                head.extend(chunk[:remaining_head])
                chunk = chunk[remaining_head:]
            if chunk:
                tail.extend(chunk)
                if len(tail) > 24_576:
                    del tail[:-24_576]
        return bytes(head + tail)


class GeminiCliLLMService(LLMService):
    """Pipecat LLM backed by the official Gemini CLI OAuth flow."""

    def __init__(
        self,
        *,
        model: str = "gemini-2.5-flash",
        system_instruction: str,
        binary: str | None = None,
        turn_timeout_secs: float = 30.0,
        client: GeminiCliClient | None = None,
        **kwargs: Any,
    ) -> None:
        settings = LLMSettings(
            model=model,
            system_instruction=system_instruction,
            temperature=None,
            max_tokens=None,
            top_p=None,
            top_k=None,
            frequency_penalty=None,
            presence_penalty=None,
            seed=None,
            filter_incomplete_user_turns=False,
            user_turn_completion_config=None,
            extra={},
        )
        super().__init__(settings=settings, **kwargs)
        self._client = client or GeminiCliClient(binary)
        self._turn_timeout_secs = turn_timeout_secs

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)

    async def stop(self, frame: EndFrame) -> None:
        await self._client.close()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._client.close()
        await super().cancel(frame)

    async def cleanup(self) -> None:
        await self._client.close()
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        await self.push_frame(LLMFullResponseStartFrame())
        await self.start_processing_metrics()
        await self.start_ttfb_metrics()
        first = True
        try:
            prompt = self._render_context(frame.context)
            async for delta in self._client.stream_completion(
                model=str(self._settings.model),
                prompt=prompt,
                timeout_secs=self._turn_timeout_secs,
            ):
                if first:
                    await self.stop_ttfb_metrics()
                    first = False
                await self._push_llm_text(delta)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.push_error(error_msg=f"Gemini CLI completion failed: {exc}", exception=exc)
        finally:
            await self.stop_ttfb_metrics()
            await self.stop_processing_metrics()
            await self.push_frame(LLMFullResponseEndFrame())

    def _render_context(self, context: LLMContext) -> str:
        turns: list[str] = []
        system = str(self._settings.system_instruction or "").strip()
        if system:
            turns.append(f"SYSTEM:\n{system}")
        for message in context.get_messages():
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "user")).upper()
            content = self._content_text(message.get("content"))
            if content:
                turns.append(f"{role}:\n{content}")
        turns.append(
            "INSTRUCTION:\nReply only with the assistant's next concise, natural, "
            "speakable telephone response. Do not use tools or Markdown."
        )
        return "\n\n".join(turns)

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
                and item.get("type") in {"text", "input_text"}
                and item.get("text")
            )
        return ""
