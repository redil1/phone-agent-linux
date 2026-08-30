"""Safe local adapter for the Codex app-server bundled with ChatGPT.

This module never reads Codex credential files. It starts the supported local
app-server process, which remains responsible for its own ChatGPT login, and
speaks its generated line-delimited JSON-RPC protocol over stdio.
"""

from __future__ import annotations

import asyncio
import json
import logging
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

logger = logging.getLogger("PhoneAgentCodexAppServer")

BUNDLED_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


class CodexAppServerError(RuntimeError):
    """The local app-server rejected or failed a request."""


def resolve_codex_binary(configured: str | None = None) -> str:
    """Resolve only executable paths; never inspect the app's credential store."""

    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise CodexAppServerError(f"configured Codex binary does not exist: {path}")
        return str(path)
    if BUNDLED_CODEX.is_file():
        return str(BUNDLED_CODEX)
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    raise CodexAppServerError("Codex app-server binary was not found")


class CodexAppServerClient:
    """Minimal asynchronous client for one isolated app-server subprocess."""

    def __init__(self, binary: str | None = None, request_timeout_secs: float = 30.0) -> None:
        self.binary = resolve_codex_binary(binary)
        self.request_timeout_secs = request_timeout_secs
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._thread_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(
            self.binary,
            "-c",
            "features.code_mode_host=false",
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="codex-app-server-rx")
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name="codex-app-server-stderr"
        )
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "phone-agent-gateway",
                    "title": "PhoneAgent Voice Gateway",
                    "version": "0.2.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})

    async def account_summary(self) -> dict[str, Any]:
        result = await self.request("account/read", {})
        account = result.get("account") or {}
        return {
            "requires_openai_auth": result.get("requiresOpenaiAuth"),
            "account_type": account.get("type"),
            "plan_type": account.get("planType"),
        }

    async def list_models(self) -> list[dict[str, Any]]:
        result = await self.request(
            "model/list", {"limit": 100, "includeHidden": False}
        )
        return list(result.get("data", []))

    async def start_thread(
        self,
        *,
        model: str,
        system_instruction: str,
        developer_instructions: str | None = None,
    ) -> str:
        """Open a thread. Callers that are not a phone call override the
        developer instructions; telling a structured-extraction run to return
        speakable prose with no Markdown works against it."""
        result = await self.request(
            "thread/start",
            {
                "model": model,
                "cwd": tempfile.gettempdir(),
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "environments": [],
                "dynamicTools": [],
                "baseInstructions": system_instruction,
                "developerInstructions": developer_instructions
                if developer_instructions is not None
                else (
                    "This is a live telephone conversation. Do not call tools, inspect files, "
                    "run commands, browse, or modify the computer. Return only concise, natural, "
                    "speakable text with no Markdown."
                ),
            },
        )
        thread_id = result.get("thread", {}).get("id")
        if not thread_id:
            raise CodexAppServerError("thread/start returned no thread id")
        self._thread_queues[thread_id] = asyncio.Queue(maxsize=256)
        return thread_id

    async def stream_turn(
        self,
        thread_id: str,
        text: str,
        *,
        effort: str,
        timeout_secs: float,
    ) -> AsyncIterator[str]:
        queue = self._thread_queues.get(thread_id)
        if queue is None:
            raise CodexAppServerError(f"unknown app-server thread: {thread_id}")

        response = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "effort": effort,
            },
        )
        turn_id = response.get("turn", {}).get("id")
        if not turn_id:
            raise CodexAppServerError("turn/start returned no turn id")

        try:
            async with asyncio.timeout(timeout_secs):
                while True:
                    message = await queue.get()
                    method = message.get("method")
                    params = message.get("params") or {}
                    message_turn_id = params.get("turnId") or params.get("turn", {}).get("id")
                    if message_turn_id != turn_id:
                        continue
                    if method == "item/agentMessage/delta":
                        delta = params.get("delta")
                        if delta:
                            yield delta
                    elif method == "turn/completed":
                        status = params.get("turn", {}).get("status")
                        if status != "completed":
                            raise CodexAppServerError(
                                f"Codex turn ended with status {status!r}"
                            )
                        return
                    elif method == "error":
                        raise CodexAppServerError(str(params.get("error") or params))
        except (asyncio.CancelledError, TimeoutError):
            await asyncio.shield(self.interrupt(thread_id, turn_id))
            raise

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        try:
            await self.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout_secs=3.0,
            )
        except Exception as exc:
            logger.warning("Codex turn interrupt failed: %s", exc)

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_secs: float | None = None,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        self._request_id += 1
        request_id = self._request_id
        future = loop.create_future()
        self._pending[request_id] = future
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            response = await asyncio.wait_for(
                future, timeout=timeout_secs or self.request_timeout_secs
            )
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise CodexAppServerError(f"{method}: {response['error']}")
        return response.get("result") or {}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
        self._reader_task = None
        self._stderr_task = None
        error = CodexAppServerError("app-server closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        self._thread_queues.clear()

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise CodexAppServerError("app-server is not running")
        encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        async with self._write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while line := await process.stdout.readline():
                message = json.loads(line)
                request_id = message.get("id")
                if request_id is not None and "method" not in message:
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                if request_id is not None and "method" in message:
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32000,
                                "message": "Tools are disabled in PhoneAgent voice mode",
                            },
                        }
                    )
                    continue
                params = message.get("params") or {}
                thread_id = params.get("threadId")
                queue = self._thread_queues.get(thread_id)
                if queue is not None:
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Codex app-server read loop failed: %s", exc)
        finally:
            error = CodexAppServerError("app-server stream ended")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _stderr_loop(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while line := await process.stderr.readline():
            logger.debug("codex app-server: %s", line.decode(errors="replace").rstrip())


class CodexAppServerLLMService(LLMService):
    """Pipecat text LLM backed by the locally authenticated Codex app-server."""

    def __init__(
        self,
        *,
        model: str = "gpt-5.6-luna",
        system_instruction: str,
        reasoning_effort: str = "low",
        binary: str | None = None,
        turn_timeout_secs: float = 30.0,
        client: CodexAppServerClient | None = None,
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
        self._client = client or CodexAppServerClient(binary)
        self._reasoning_effort = reasoning_effort
        self._turn_timeout_secs = turn_timeout_secs
        self._thread_id: str | None = None

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self._client.start()
        account = await self._client.account_summary()
        if account.get("account_type") != "chatgpt":
            raise CodexAppServerError("Codex is not logged in with a ChatGPT account")
        models = await self._client.list_models()
        model = str(self._settings.model)
        if model not in {entry.get("id") for entry in models}:
            raise CodexAppServerError(f"Codex model is unavailable for this account: {model}")
        self._thread_id = await self._client.start_thread(
            model=model,
            system_instruction=str(self._settings.system_instruction),
        )

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
            prompt = self._latest_user_text(frame.context)
            if not prompt:
                raise CodexAppServerError("no user text available for Codex turn")
            if self._thread_id is None:
                raise CodexAppServerError("Codex thread is not initialized")
            async for delta in self._client.stream_turn(
                self._thread_id,
                prompt,
                effort=self._reasoning_effort,
                timeout_secs=self._turn_timeout_secs,
            ):
                if first:
                    await self.stop_ttfb_metrics()
                    first = False
                await self._push_llm_text(delta)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.push_error(error_msg=f"Codex completion failed: {exc}", exception=exc)
        finally:
            await self.stop_ttfb_metrics()
            await self.stop_processing_metrics()
            await self.push_frame(LLMFullResponseEndFrame())

    @staticmethod
    def _latest_user_text(context: LLMContext) -> str:
        for message in reversed(context.get_messages()):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") in {"text", "input_text"}
                ]
                return " ".join(part for part in parts if part)
        return ""

