"""User-defined Realtime tools that execute real work.

Drop a Python file into ``~/.config/phone-agent/tools/`` and decorate a function
with :func:`realtime_tool`. The agent can then call it on a live phone call to
query a database, hit an internal API, or take an action:

    from phone_agent_gateway.ai_bridge.tasks.tool_registry import realtime_tool

    @realtime_tool(
        name="lookup_subscriber",
        description="Find an existing subscriber by phone number.",
        params={"phone": {"type": "string", "description": "E.164 number"}},
        required=["phone"],
        timeout_secs=1.5,
    )
    async def lookup_subscriber(phone: str) -> dict:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT plan FROM subs WHERE phone=$1", phone)
        return {"found": bool(row), "plan": row["plan"] if row else None}

A tool is offered to the model only when the active task contract also lists it
in ``allowed_tools``, so the contract stays the single place that decides what
this call is permitted to do.

Two rules the phone imposes on every handler:

* **Be fast.** A tool call already costs a second model inference before the
  caller hears anything. Anything past a second or so is silence on the line.
* **Never claim more than you did.** Return what actually happened. A handler
  that reports success it did not achieve makes the agent lie to a customer.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("RealtimeToolRegistry")

USER_TOOLS_DIR = Path.home() / ".config" / "phone-agent" / "tools"
# Past this the caller is listening to nothing. Handlers should be far quicker.
DEFAULT_TIMEOUT_SECS = 2.0
MAX_TIMEOUT_SECS = 10.0


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One user-defined tool and everything needed to offer and run it."""

    name: str
    description: str
    handler: Callable[..., Any]
    params: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    timeout_secs: float = DEFAULT_TIMEOUT_SECS

    @property
    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": dict(self.params),
                "required": list(self.required),
                "additionalProperties": False,
            },
        }


_REGISTRY: dict[str, ToolSpec] = {}
_LOADED: dict[Path, float] = {}


def realtime_tool(
    *,
    name: str,
    description: str,
    params: dict[str, Any] | None = None,
    required: list[str] | None = None,
    timeout_secs: float = DEFAULT_TIMEOUT_SECS,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register one function as a tool the agent may call during a call."""

    if not name or not name.replace("_", "").isalnum():
        raise ValueError(f"tool name must be alphanumeric with underscores: {name!r}")
    if not description.strip():
        raise ValueError(f"tool {name!r} needs a description; the model chooses by it")
    properties = dict(params or {})
    mandatory = tuple(required or ())
    for field_name in mandatory:
        if field_name not in properties:
            raise ValueError(f"tool {name!r} requires {field_name!r} but never declares it")
    timeout = max(0.1, min(float(timeout_secs), MAX_TIMEOUT_SECS))

    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY and _REGISTRY[name].handler is not handler:
            logger.warning("Realtime tool %r redefined; using the newest definition", name)
        _REGISTRY[name] = ToolSpec(
            name=name,
            description=description.strip(),
            handler=handler,
            params=properties,
            required=mandatory,
            timeout_secs=timeout,
        )
        logger.info("Registered Realtime tool %r timeout=%.1fs", name, timeout)
        return handler

    return decorator


def registered_tools() -> dict[str, ToolSpec]:
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Drop every registered tool. For tests and for a clean reload."""

    _REGISTRY.clear()
    _LOADED.clear()


def load_user_tools(directory: Path | None = None) -> dict[str, str]:
    """Import tool modules from ``directory``, reloading ones that changed.

    Returns a per-file status so a broken tool file is visible rather than
    silently absent. An import failure never propagates: one bad tool file must
    not stop a call from starting.
    """

    target = directory if directory is not None else USER_TOOLS_DIR
    statuses: dict[str, str] = {}
    if not target.is_dir():
        return statuses
    for path in sorted(target.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            modified = path.stat().st_mtime
        except OSError as exc:
            statuses[path.name] = f"unreadable: {exc}"
            continue
        if _LOADED.get(path) == modified:
            statuses[path.name] = "unchanged"
            continue
        module_name = f"phone_agent_user_tools.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("no import spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            _LOADED[path] = modified
            statuses[path.name] = "loaded"
        except Exception as exc:
            sys.modules.pop(module_name, None)
            logger.error("Could not load tool file %s: %s", path, exc, exc_info=True)
            statuses[path.name] = f"failed: {type(exc).__name__}: {exc}"
    return statuses


async def run_tool(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool with its timeout, as keyword arguments.

    A handler that hangs would otherwise leave the caller in unbounded silence,
    because the model cannot produce the spoken answer until the result returns.
    """

    accepted = _accepted_arguments(spec.handler, arguments)
    try:
        if inspect.iscoroutinefunction(spec.handler):
            return await asyncio.wait_for(spec.handler(**accepted), timeout=spec.timeout_secs)
        return await asyncio.wait_for(
            asyncio.to_thread(spec.handler, **accepted), timeout=spec.timeout_secs
        )
    except TimeoutError:
        logger.error("Realtime tool %r timed out after %.1fs", spec.name, spec.timeout_secs)
        return {
            "error": "timeout",
            "say": (
                "Tell the caller you could not confirm that right now and offer to "
                "follow up. Never state a result you did not get."
            ),
        }


def _accepted_arguments(
    handler: Callable[..., Any], arguments: dict[str, Any]
) -> dict[str, Any]:
    """Drop arguments the handler does not declare, unless it takes **kwargs.

    The model can invent a plausible extra field; that should not raise a
    TypeError into the middle of a phone call.
    """

    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return dict(arguments)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        return dict(arguments)
    return {key: value for key, value in arguments.items() if key in signature.parameters}
