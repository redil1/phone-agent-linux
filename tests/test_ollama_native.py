"""Native Ollama protocol, no-thinking, and cancellation tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from aiohttp import web
from pipecat.frames.frames import Frame, LLMContextFrame, LLMFullResponseEndFrame, LLMTextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from phone_agent_gateway.ai_bridge.ollama_native import (
    OllamaNativeClient,
    OllamaNativeLLMService,
    OllamaPrewarmResult,
    OllamaStreamEvent,
    normalize_ollama_base_url,
)


async def start_server(
    handler: Callable[[web.Request], web.StreamResponse],
) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/api/chat", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None
    port = server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def test_normalize_ollama_base_url_accepts_legacy_v1() -> None:
    assert (
        normalize_ollama_base_url("http://127.0.0.1:11434/v1/")
        == "http://127.0.0.1:11434"
    )


@pytest.mark.asyncio
async def test_prewarm_loads_without_generation_and_keeps_model_resident() -> None:
    captured: dict = {}

    async def handler(request: web.Request) -> web.Response:
        captured.update(await request.json())
        return web.json_response(
            {
                "model": "test-model",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "load",
            }
        )

    runner, base_url = await start_server(handler)
    client = OllamaNativeClient(base_url=base_url)
    try:
        result = await client.prewarm(
            model="test-model",
            keep_alive="-1",
            options={"num_ctx": 8192},
        )
    finally:
        await client.close()
        await runner.cleanup()

    assert captured == {
        "model": "test-model",
        "messages": [],
        "stream": False,
        "think": False,
        "keep_alive": -1,
        "options": {"num_ctx": 8192},
    }
    assert result.done_reason == "load"


@pytest.mark.asyncio
async def test_stream_chat_uses_native_no_thinking_ndjson_and_reports_usage() -> None:
    captured: dict = {}

    async def handler(request: web.Request) -> web.StreamResponse:
        captured.update(await request.json())
        response = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        for event in (
            {"message": {"role": "assistant", "content": "Hello"}, "done": False},
            {
                "message": {"role": "assistant", "content": " there"},
                "done": True,
                "prompt_eval_count": 7,
                "eval_count": 2,
            },
        ):
            await response.write(json.dumps(event).encode() + b"\n")
        await response.write_eof()
        return response

    runner, base_url = await start_server(handler)
    client = OllamaNativeClient(base_url=base_url)
    try:
        events = [
            event
            async for event in client.stream_chat(
                model="test-model",
                messages=[{"role": "user", "content": "Hello?"}],
                keep_alive="-1",
                think=False,
                options={"temperature": 0.4, "num_predict": 192, "num_ctx": 8192},
            )
        ]
    finally:
        await client.close()
        await runner.cleanup()

    assert "".join(event.content for event in events) == "Hello there"
    assert events[-1].done is True
    assert events[-1].prompt_tokens == 7
    assert events[-1].completion_tokens == 2
    assert captured["stream"] is True
    assert captured["think"] is False
    assert captured["keep_alive"] == -1


@pytest.mark.asyncio
async def test_stream_chat_releases_request_immediately_when_cancelled() -> None:
    first_sent = asyncio.Event()
    release_server = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        await request.json()
        response = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        await response.write(
            json.dumps(
                {"message": {"role": "assistant", "content": "partial"}, "done": False}
            ).encode()
            + b"\n"
        )
        first_sent.set()
        await release_server.wait()
        try:
            await response.write(
                json.dumps(
                    {"message": {"role": "assistant", "content": "late"}, "done": True}
                ).encode()
                + b"\n"
            )
        except ConnectionResetError:
            pass
        return response

    runner, base_url = await start_server(handler)
    client = OllamaNativeClient(base_url=base_url)

    async def consume() -> None:
        async for _event in client.stream_chat(
            model="test-model",
            messages=[{"role": "user", "content": "continue"}],
            keep_alive="-1",
            think=False,
            options={},
        ):
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(first_sent.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        release_server.set()
        await client.close()
        await runner.cleanup()


def test_context_conversion_preserves_multiturn_text_without_reasoning_prompt() -> None:
    service = OllamaNativeLLMService(
        model="test-model",
        system_instruction="Speak naturally.",
        prewarm_on_start=False,
    )
    context = LLMContext(
        [
            {"role": "assistant", "content": "Hello."},
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Can you help?"}],
            },
        ]
    )

    assert service._context_messages(context) == [
        {"role": "system", "content": "Speak naturally."},
        {"role": "assistant", "content": "Hello."},
        {"role": "user", "content": "Can you help?"},
    ]


class FakeOllamaClient:
    def __init__(self) -> None:
        self.started = False
        self.prewarm_options: dict[str, Any] | None = None
        self.stream_arguments: dict[str, Any] | None = None
        self.cancelled = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def prewarm(
        self,
        *,
        model: str,
        keep_alive: str,
        options: dict[str, Any] | None = None,
    ) -> OllamaPrewarmResult:
        self.prewarm_options = options
        return OllamaPrewarmResult(model=model, elapsed_ms=1.0, done_reason="load")

    async def stream_chat(self, **kwargs: Any) -> AsyncIterator[OllamaStreamEvent]:
        self.stream_arguments = kwargs
        yield OllamaStreamEvent(content="Hello")
        yield OllamaStreamEvent(done=True, prompt_tokens=5, completion_tokens=1)

    async def cancel_active(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True


class TextSink(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.completed = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self.text.append(frame.text)
        if isinstance(frame, LLMFullResponseEndFrame):
            self.completed.set()
        await self.push_frame(frame, direction)


@pytest.mark.asyncio
async def test_real_pipecat_worker_streams_native_ollama_text() -> None:
    client = FakeOllamaClient()
    service = OllamaNativeLLMService(
        model="test-model",
        client=client,  # type: ignore[arg-type]
        think=False,
        keep_alive="-1",
        num_ctx=8192,
    )
    sink = TextSink()
    worker = PipelineWorker(
        Pipeline([service, sink]),
        params=PipelineParams(),
        enable_rtvi=False,
    )
    started = asyncio.Event()

    @worker.event_handler("on_pipeline_started")
    async def on_pipeline_started(_worker, _frame) -> None:
        started.set()

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    runner_task = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await worker.queue_frame(
            LLMContextFrame(LLMContext([{"role": "user", "content": "Say hello"}]))
        )
        await asyncio.wait_for(sink.completed.wait(), timeout=2.0)
        assert "".join(sink.text) == "Hello"
        assert client.prewarm_options == {
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "num_predict": 192,
            "num_ctx": 8192,
        }
        assert client.stream_arguments is not None
        assert client.stream_arguments["think"] is False
        assert client.stream_arguments["keep_alive"] == "-1"
        assert client.stream_arguments["options"] == client.prewarm_options
    finally:
        await worker.stop_when_done()
        await asyncio.wait_for(runner_task, timeout=3.0)


@pytest.mark.asyncio
async def test_prewarm_with_messages_populates_the_prompt_cache() -> None:
    """Loading weights alone leaves the first caller turn paying full prompt cost."""

    captured: dict[str, Any] = {}

    class _Client:
        async def start(self) -> None:
            return None

        async def prewarm(self, **kwargs: Any) -> Any:
            captured.update(kwargs)

            class _R:
                elapsed_ms = 42.0

            return _R()

    service = OllamaNativeLLMService(model="qwen3.5:4b-mlx", client=_Client())
    elapsed = await service.warm_prompt_prefix("You are Adam at OXzoon.")

    assert elapsed == 42.0
    messages = captured["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are Adam at OXzoon."


@pytest.mark.asyncio
async def test_warm_prompt_prefix_ignores_an_empty_prompt() -> None:
    class _Client:
        async def start(self) -> None:
            return None

        async def prewarm(self, **kwargs: Any) -> Any:
            raise AssertionError("must not warm an empty prefix")

    service = OllamaNativeLLMService(model="qwen3.5:4b-mlx", client=_Client())
    assert await service.warm_prompt_prefix("   ") is None


@pytest.mark.asyncio
async def test_prewarm_with_messages_forces_a_single_token() -> None:
    """The warm pass must cache the prefix, not generate a whole reply."""

    captured: dict = {}

    async def handler(request: web.Request) -> web.Response:
        captured.update(await request.json())
        return web.json_response(
            {
                "model": "test-model",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
            }
        )

    runner, base_url = await start_server(handler)
    client = OllamaNativeClient(base_url=base_url)
    try:
        await client.prewarm(
            model="test-model",
            keep_alive="-1",
            options={"num_ctx": 8192, "num_predict": 192},
            messages=[{"role": "system", "content": "You are Adam."}],
        )
    finally:
        await client.close()
        await runner.cleanup()

    assert captured["messages"] == [{"role": "system", "content": "You are Adam."}]
    assert captured["options"]["num_predict"] == 1
    assert captured["options"]["num_ctx"] == 8192
