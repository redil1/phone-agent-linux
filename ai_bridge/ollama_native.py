"""Native low-latency Ollama streaming service for Pipecat.

The OpenAI-compatible Ollama endpoint does not currently forward ``think=false``
reliably for the selected Qwen model. This adapter uses Ollama's documented native
``/api/chat`` NDJSON stream so reasoning can be disabled explicitly and partial
text reaches TTS as soon as it is generated.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    StartFrame,
)
from pipecat.metrics.metrics import LLMTokenUsage
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings

logger = logging.getLogger("PhoneAgentOllamaNative")

MAX_ERROR_BYTES = 8_192
MAX_STREAM_LINE_BYTES = 1_048_576
LatencySink = Callable[[dict[str, Any]], Awaitable[None] | None]


class OllamaNativeError(RuntimeError):
    """The local Ollama API could not complete a safe streaming turn."""


@dataclass(frozen=True, slots=True)
class OllamaStreamEvent:
    """One normalized event from Ollama's NDJSON chat stream."""

    content: str = ""
    done: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    load_ms: float = 0.0
    prompt_eval_ms: float = 0.0
    eval_ms: float = 0.0
    total_ms: float = 0.0
    # Ollama returns structured calls rather than text. They are normalized into
    # the cascade's tool block downstream so one processor serves every model.
    tool_calls: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class OllamaPrewarmResult:
    """Measured model-residency result used by host readiness checks."""

    model: str
    elapsed_ms: float
    done_reason: str


def normalize_ollama_base_url(value: str) -> str:
    """Return the native Ollama origin, accepting a legacy trailing ``/v1``."""

    candidate = value.strip().rstrip("/")
    if not candidate:
        raise OllamaNativeError("Ollama base URL is empty")
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise OllamaNativeError("Ollama base URL must be an HTTP(S) origin")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    if path:
        raise OllamaNativeError("Ollama base URL must not contain a path other than /v1")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def ollama_keep_alive_value(value: str) -> str | int:
    """Encode numeric durations as numbers and unit durations as strings."""

    normalized = value.strip()
    if normalized.lstrip("+-").isdigit():
        return int(normalized)
    return normalized


# Some models document sampling settings that are not preferences but part of
# how they were trained, and ignoring them degrades the model rather than merely
# changing its style. PhoneLLM Alpha 1 is explicit about this: "set temperature
# to 0 and disable thinking. These two settings align with how the model was
# trained." Measured against the 4.3k-token persona on this machine, temperature
# 0.7 gave a TTFT p50 of 2782 ms with spikes past 3 s; temperature 0 gave 235 ms.
# Thinking is already disabled by default for every Ollama model here.
REQUIRED_MODEL_OPTIONS: dict[str, dict[str, Any]] = {
    # PhoneLLM documents deterministic sampling and no thinking. Its 65k
    # maximum context is a capability, not a required allocation; forcing it
    # ignored the operator's num_ctx setting and consumed scarce VRAM alongside
    # Whisper and Kokoro.
    "phonellm-alpha-1": {"temperature": 0.0},
}


def required_model_options(model: str) -> dict[str, Any]:
    """Sampling settings a model documents as required, matched on its name.

    Matched as a substring so a repository path, tag or quantization suffix
    still resolves -- Ollama names the same weights
    ``hf.co/EryriLabs/phonellm-alpha-1-GGUF:Q4_K_M``.
    """

    name = str(model or "").lower()
    for marker, options in REQUIRED_MODEL_OPTIONS.items():
        if marker in name:
            return dict(options)
    return {}


class OllamaNativeClient:
    """Bounded asynchronous client for Ollama's native chat endpoint."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        connect_timeout_secs: float = 3.0,
        turn_timeout_secs: float = 30.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = normalize_ollama_base_url(base_url)
        self._connect_timeout_secs = connect_timeout_secs
        self._turn_timeout_secs = turn_timeout_secs
        self._session = session
        self._owns_session = session is None
        self._request_lock = asyncio.Lock()
        self._active_response: aiohttp.ClientResponse | None = None

    async def start(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self._connect_timeout_secs,
            sock_connect=self._connect_timeout_secs,
            sock_read=self._turn_timeout_secs,
        )
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(limit=1),
            raise_for_status=False,
        )
        self._owns_session = True

    async def prewarm(
        self,
        *,
        model: str,
        keep_alive: str,
        options: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> OllamaPrewarmResult:
        """Load a model and hold it resident for ``keep_alive``.

        Passing ``messages`` also runs that prefix through the model once, which
        populates Ollama's prompt cache. Loading the weights alone leaves the
        cache empty, so the first caller turn pays full prompt processing for
        the persona and task contract - measured at 3.2 s against 0.9 s once the
        prefix is cached.
        """

        await self.start()
        session = self._require_session()
        loop = asyncio.get_running_loop()
        started = loop.time()
        warm_options = dict(options or {})
        if messages:
            # One token is enough to force the prefix through the model; the
            # generated text is discarded.
            warm_options["num_predict"] = 1
        payload = {
            "model": model,
            "messages": messages or [],
            "stream": False,
            "think": False,
            "keep_alive": ollama_keep_alive_value(keep_alive),
            "options": warm_options,
        }
        async with self._request_lock:
            async with session.post(f"{self.base_url}/api/chat", json=payload) as response:
                body = await self._read_json_response(response)
        if not body.get("done"):
            raise OllamaNativeError("Ollama prewarm response was not complete")
        return OllamaPrewarmResult(
            model=model,
            elapsed_ms=(loop.time() - started) * 1000,
            done_reason=str(body.get("done_reason") or ""),
        )

    async def list_running_models(self) -> tuple[str, ...]:
        """Return models currently holding Ollama CPU/GPU memory."""

        await self.start()
        session = self._require_session()
        async with session.get(f"{self.base_url}/api/ps") as response:
            body = await self._read_json_response(response)
        models = body.get("models") or []
        if not isinstance(models, list):
            raise OllamaNativeError("Ollama /api/ps returned an invalid model list")
        return tuple(
            name
            for item in models
            if isinstance(item, dict)
            and (name := str(item.get("name") or item.get("model") or "").strip())
        )

    async def unload(self, model: str) -> None:
        """Release one resident model through Ollama's keep_alive=0 contract."""

        name = str(model or "").strip()
        if not name:
            return
        await self.start()
        session = self._require_session()
        payload = {"model": name, "stream": False, "keep_alive": 0}
        async with self._request_lock:
            async with session.post(f"{self.base_url}/api/generate", json=payload) as response:
                await self._read_json_response(response)

    async def stream_chat(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        keep_alive: str,
        think: bool,
        options: dict[str, Any],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AsyncIterator[OllamaStreamEvent]:
        """Yield text, tool calls and final usage from one native Ollama turn."""

        await self.start()
        session = self._require_session()
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": True,
            "think": think,
            "keep_alive": ollama_keep_alive_value(keep_alive),
            "options": options,
        }
        if tools:
            # Ollama takes OpenAI-shaped definitions. The catalog stores the
            # flat Realtime shape, so it is wrapped here rather than making
            # every caller know the difference.
            payload["tools"] = [
                tool if "function" in tool else {"type": "function", "function": tool}
                for tool in tools
            ]
        completed = False
        async with self._request_lock:
            response = await session.post(f"{self.base_url}/api/chat", json=payload)
            self._active_response = response
            try:
                if response.status == 400 and tools:
                    raw = await response.content.read(MAX_ERROR_BYTES)
                    detail = raw.decode(errors="replace").strip()
                    if "does not support tools" in detail.lower():
                        logger.warning(
                            "Ollama model %s does not support native tools; falling back to text stream",
                            model,
                        )
                        payload.pop("tools", None)
                        response.close()
                        response = await session.post(f"{self.base_url}/api/chat", json=payload)
                        self._active_response = response
                if response.status != 200:
                    await self._raise_response_error(response)
                while raw_line := await response.content.readline():
                    if len(raw_line) > MAX_STREAM_LINE_BYTES:
                        raise OllamaNativeError("Ollama stream event exceeded the size limit")
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        raise OllamaNativeError("Ollama returned invalid NDJSON") from exc
                    if error := event.get("error"):
                        raise OllamaNativeError(f"Ollama stream failed: {str(error)[:2000]}")
                    message = event.get("message") or {}
                    content = message.get("content") or ""
                    if not isinstance(content, str):
                        raise OllamaNativeError("Ollama returned non-text message content")
                    done = bool(event.get("done"))
                    if done:
                        completed = True
                    yield OllamaStreamEvent(
                        content=content,
                        done=done,
                        prompt_tokens=int(event.get("prompt_eval_count") or 0),
                        completion_tokens=int(event.get("eval_count") or 0),
                        load_ms=float(event.get("load_duration") or 0) / 1_000_000,
                        prompt_eval_ms=(
                            float(event.get("prompt_eval_duration") or 0) / 1_000_000
                        ),
                        eval_ms=float(event.get("eval_duration") or 0) / 1_000_000,
                        total_ms=float(event.get("total_duration") or 0) / 1_000_000,
                        tool_calls=tuple(message.get("tool_calls") or ()),
                    )
                if not completed:
                    raise OllamaNativeError("Ollama closed the stream before completion")
            except asyncio.CancelledError:
                response.close()
                raise
            finally:
                response.close()
                if self._active_response is response:
                    self._active_response = None

    async def cancel_active(self) -> None:
        """Close the active HTTP response so Ollama observes client cancellation."""

        response = self._active_response
        if response is not None:
            response.close()
            await asyncio.sleep(0)

    async def close(self) -> None:
        await self.cancel_active()
        session = self._session
        self._session = None
        if session is not None and self._owns_session and not session.closed:
            await session.close()

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise OllamaNativeError("Ollama client is not started")
        return self._session

    async def _read_json_response(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        if response.status != 200:
            await self._raise_response_error(response)
        raw = await response.content.read(MAX_ERROR_BYTES + 1)
        if len(raw) > MAX_ERROR_BYTES:
            raise OllamaNativeError("Ollama response exceeded the size limit")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OllamaNativeError("Ollama returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise OllamaNativeError("Ollama returned a non-object response")
        if error := value.get("error"):
            raise OllamaNativeError(f"Ollama request failed: {str(error)[:2000]}")
        return value

    async def _raise_response_error(self, response: aiohttp.ClientResponse) -> None:
        raw = await response.content.read(MAX_ERROR_BYTES)
        detail = raw.decode(errors="replace").strip()[:2000]
        raise OllamaNativeError(f"Ollama HTTP {response.status}: {detail or 'no detail'}")


async def unload_inactive_ollama_models(
    *,
    base_url: str,
    keep_model: str | None,
) -> tuple[str, ...]:
    """Unload every resident Ollama model except the selected active model."""

    client = OllamaNativeClient(base_url=base_url, turn_timeout_secs=15.0)
    unloaded: list[str] = []
    try:
        running = await client.list_running_models()
        keep = str(keep_model or "").strip()
        for model in running:
            if keep and model == keep:
                continue
            await client.unload(model)
            unloaded.append(model)
    finally:
        await client.close()
    return tuple(unloaded)


def _render_tool_call(call: dict[str, Any]) -> str:
    """Render one Ollama tool call as the cascade's delimited block."""

    function = call.get("function") or {}
    name = str(function.get("name") or "").strip()
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    payload = json.dumps(
        {"name": name, "arguments": arguments or {}}, ensure_ascii=False
    )
    return f"<tool_call>{payload}</tool_call>"


class OllamaNativeLLMService(LLMService):
    """Pipecat LLM service using native, no-thinking Ollama token streaming."""

    def __init__(
        self,
        *,
        model: str = "qwen2.5:3b",
        base_url: str = "http://127.0.0.1:11434",
        system_instruction: str | None = None,
        temperature: float = 0.7,
        top_p: float = 0.8,
        top_k: int = 20,
        min_p: float = 0.0,
        presence_penalty: float = 0.0,
        num_predict: int = 192,
        num_ctx: int = 8192,
        think: bool = False,
        keep_alive: str = "-1",
        turn_timeout_secs: float = 30.0,
        prewarm_on_start: bool = True,
        client: OllamaNativeClient | None = None,
        **kwargs: Any,
    ) -> None:
        settings = LLMSettings(
            model=model,
            system_instruction=system_instruction,
            temperature=temperature,
            max_tokens=num_predict,
            top_p=top_p,
            top_k=top_k,
            frequency_penalty=None,
            presence_penalty=presence_penalty,
            seed=None,
            filter_incomplete_user_turns=False,
            user_turn_completion_config=None,
            extra={},
        )
        super().__init__(settings=settings, **kwargs)
        self._client = client or OllamaNativeClient(
            base_url=base_url,
            turn_timeout_secs=turn_timeout_secs,
        )
        self._temperature = temperature
        self._top_p = top_p
        self._top_k = top_k
        self._min_p = min_p
        self._presence_penalty = presence_penalty
        self._num_predict = num_predict
        self._num_ctx = num_ctx
        self._think = think
        self._keep_alive = keep_alive
        self._prewarm_on_start = prewarm_on_start
        self._latency_sink: LatencySink | None = None
        self._generation_sequence = 0
        # Populated per call from the cascade tool catalog. Empty means this
        # model is asked nothing about tools, exactly as before.
        self._tool_definitions: list[dict[str, Any]] = []

    def set_latency_sink(self, sink: LatencySink | None) -> None:
        self._latency_sink = sink

    async def _emit_latency(self, event: dict[str, Any]) -> None:
        if self._latency_sink is None:
            return
        try:
            result = self._latency_sink(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("could not publish Ollama latency metric", exc_info=True)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self._client.start()
        if self._prewarm_on_start:
            await self._client.prewarm(
                model=str(self._settings.model),
                keep_alive=self._keep_alive,
                options=self._options(),
            )

    async def warm_prompt_prefix(self, system_prompt: str) -> float | None:
        """Cache the exact system prefix this call will use.

        Called while the opening greeting is being spoken, so the first caller
        turn hits a warm prompt cache instead of paying full prompt processing
        for the persona and task contract.
        """

        if not system_prompt.strip():
            return None
        result = await self._client.prewarm(
            model=str(self._settings.model),
            keep_alive=self._keep_alive,
            options=self._options(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "."},
            ],
        )
        return result.elapsed_ms

    async def stop(self, frame: EndFrame) -> None:
        await self._client.close()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._client.cancel_active()
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

        self._generation_sequence += 1
        generation = self._generation_sequence
        loop = asyncio.get_running_loop()
        started = loop.time()
        ttft_ms: float | None = None
        completed_event: OllamaStreamEvent | None = None
        await self._emit_latency(
            {
                "type": "latency_metric",
                "stage": "llm_started",
                "provider": "ollama",
                "model": str(self._settings.model),
                "generation": generation,
            }
        )
        await self.push_frame(LLMFullResponseStartFrame())
        await self.start_processing_metrics()
        await self.start_ttfb_metrics()
        first = True
        try:
            async for event in self._stream_context(frame.context):
                if event.content:
                    if first:
                        await self.stop_ttfb_metrics()
                        ttft_ms = (loop.time() - started) * 1000
                        await self._emit_latency(
                            {
                                "type": "latency_metric",
                                "stage": "llm_ttft",
                                "provider": "ollama",
                                "model": str(self._settings.model),
                                "generation": generation,
                                "milliseconds": round(ttft_ms, 1),
                            }
                        )
                        first = False
                    await self._push_llm_text(event.content)
                if event.tool_calls:
                    # Ollama answers with structured calls while the local
                    # bridges can only emit text. Rendering both as the same
                    # block keeps one executor downstream instead of a second
                    # tool path that could drift from the guarded one.
                    if first:
                        await self.stop_ttfb_metrics()
                        ttft_ms = (loop.time() - started) * 1000
                        await self._emit_latency(
                            {
                                "type": "latency_metric",
                                "stage": "llm_ttft",
                                "provider": "ollama",
                                "model": str(self._settings.model),
                                "generation": generation,
                                "milliseconds": round(ttft_ms, 1),
                            }
                        )
                        first = False
                    for call in event.tool_calls:
                        await self._push_llm_text(_render_tool_call(call))
                if event.done:
                    completed_event = event
                    await self.start_llm_usage_metrics(
                        LLMTokenUsage(
                            prompt_tokens=event.prompt_tokens,
                            completion_tokens=event.completion_tokens,
                            total_tokens=event.prompt_tokens + event.completion_tokens,
                            reasoning_tokens=0 if not self._think else None,
                        )
                    )
        except asyncio.CancelledError:
            await asyncio.shield(self._client.cancel_active())
            raise
        except Exception as exc:
            await self.push_error(
                error_msg=f"Native Ollama completion failed: {exc}",
                exception=exc,
            )
        finally:
            await self.stop_ttfb_metrics()
            await self.stop_processing_metrics()
            if completed_event is not None:
                await self._emit_latency(
                    {
                        "type": "latency_metric",
                        "stage": "llm_completed",
                        "provider": "ollama",
                        "model": str(self._settings.model),
                        "generation": generation,
                        "ttft_ms": round(ttft_ms or 0.0, 1),
                        "wall_ms": round((loop.time() - started) * 1000, 1),
                        "load_ms": round(completed_event.load_ms, 1),
                        "prompt_eval_ms": round(completed_event.prompt_eval_ms, 1),
                        "decode_ms": round(completed_event.eval_ms, 1),
                        "ollama_total_ms": round(completed_event.total_ms, 1),
                        "prompt_tokens": completed_event.prompt_tokens,
                        "completion_tokens": completed_event.completion_tokens,
                    }
                )
            await self.push_frame(LLMFullResponseEndFrame())

    async def run_inference(
        self,
        context: LLMContext,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
    ) -> str | None:
        messages = self._context_messages(context, system_instruction=system_instruction)
        parts: list[str] = []
        async for event in self._client.stream_chat(
            model=str(self._settings.model),
            messages=messages,
            keep_alive=self._keep_alive,
            think=self._think,
            options=self._options(max_tokens=max_tokens),
        ):
            if event.content:
                parts.append(event.content)
        text = "".join(parts).strip()
        return text or None

    def set_tool_definitions(self, definitions: Sequence[dict[str, Any]]) -> None:
        """Publish the call's tool catalog to Ollama's native function calling."""

        self._tool_definitions = list(definitions or ())

    async def _stream_context(self, context: LLMContext) -> AsyncIterator[OllamaStreamEvent]:
        messages = self._context_messages(context)
        async for event in self._client.stream_chat(
            model=str(self._settings.model),
            messages=messages,
            keep_alive=self._keep_alive,
            think=self._think,
            options=self._options(),
            tools=self._tool_definitions or None,
        ):
            yield event

    def _options(self, *, max_tokens: int | None = None) -> dict[str, Any]:
        options = {
            "temperature": self._temperature,
            "top_p": self._top_p,
            "top_k": self._top_k,
            "min_p": self._min_p,
            "presence_penalty": self._presence_penalty,
            "num_predict": max_tokens or self._num_predict,
            "num_ctx": self._num_ctx,
        }
        # A model's documented training-time settings win over the generic
        # defaults, which are tuned for a different kind of model.
        options.update(required_model_options(str(self._settings.model)))
        return options

    def _context_messages(
        self,
        context: LLMContext,
        *,
        system_instruction: str | None = None,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        instruction = system_instruction
        if instruction is None:
            instruction = str(self._settings.system_instruction or "").strip()
        if instruction:
            messages.append({"role": "system", "content": instruction})
        for message in context.get_messages():
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user").lower()
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            content = self._content_text(message.get("content"))
            if content:
                messages.append({"role": role, "content": content})
        if not any(message["role"] == "user" for message in messages):
            raise OllamaNativeError("Ollama context contains no user message")
        return messages

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
