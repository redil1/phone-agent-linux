"""User tools execute real work without ever endangering the call."""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

import pytest
from phone_agent_gateway.ai_bridge.tasks.tool_catalog import (
    build_tool_catalog,
    execute_tool,
    unimplemented_tools,
)
from phone_agent_gateway.ai_bridge.tasks.tool_registry import (
    ToolSpec,
    clear_registry,
    load_user_tools,
    realtime_tool,
    registered_tools,
    run_tool,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def contract(**overrides) -> dict:
    base = {
        "id": "t",
        "allowed_tools": ["lookup_subscriber", "callback_schedule"],
        "approval_required": [],
        "knowledge": {},
    }
    return {**base, **overrides}


class _Task:
    """Minimal TaskRuntime stand-in; the registry never touches slot logic."""

    slots: ClassVar[tuple] = ()

    def __init__(self) -> None:
        self.state: dict = {}

    def record(self, key, value):
        self.state[key] = value

    def missing_slots(self):
        return ()


def run(tools, name: str, **arguments) -> dict:
    return json.loads(asyncio.run(execute_tool(tools, name, json.dumps(arguments))))


def test_a_registered_async_tool_is_offered_and_executed() -> None:
    @realtime_tool(
        name="lookup_subscriber",
        description="Find a subscriber.",
        params={"phone": {"type": "string", "description": "E.164"}},
        required=["phone"],
    )
    async def lookup_subscriber(phone: str) -> dict:
        return {"found": True, "phone": phone, "plan": "advanced"}

    tools = build_tool_catalog(contract(), _Task())
    assert "lookup_subscriber" in tools
    definition = tools["lookup_subscriber"].definition
    assert definition["type"] == "function"
    assert definition["parameters"]["required"] == ["phone"]

    assert run(tools, "lookup_subscriber", phone="+33123")["plan"] == "advanced"


def test_a_synchronous_tool_works_too() -> None:
    @realtime_tool(name="lookup_subscriber", description="Sync lookup.")
    def lookup_subscriber() -> dict:
        return {"found": False}

    tools = build_tool_catalog(contract(), _Task())
    assert run(tools, "lookup_subscriber")["found"] is False


def test_the_contract_still_decides_what_the_call_may_use() -> None:
    """Importable is not the same as permitted."""

    @realtime_tool(name="wire_transfer", description="Move money.")
    def wire_transfer() -> dict:
        return {"sent": True}

    assert "wire_transfer" in registered_tools()
    tools = build_tool_catalog(contract(), _Task())
    assert "wire_transfer" not in tools


def test_a_hanging_tool_cannot_leave_the_caller_in_silence() -> None:
    @realtime_tool(name="lookup_subscriber", description="Slow.", timeout_secs=0.1)
    async def lookup_subscriber() -> dict:
        await asyncio.sleep(30)
        return {"never": True}

    result = run(build_tool_catalog(contract(), _Task()), "lookup_subscriber")
    assert result["error"] == "timeout"
    assert "Never state a result you did not get" in result["say"]


def test_a_raising_tool_becomes_a_result_not_a_dropped_call() -> None:
    @realtime_tool(name="lookup_subscriber", description="Broken.")
    def lookup_subscriber() -> dict:
        raise RuntimeError("database is down")

    result = run(build_tool_catalog(contract(), _Task()), "lookup_subscriber")
    assert "database is down" in result["error"]


def test_an_invented_argument_does_not_raise_into_the_call() -> None:
    """The model can add a plausible field the handler never declared."""

    @realtime_tool(
        name="lookup_subscriber",
        description="Find.",
        params={"phone": {"type": "string", "description": "E.164"}},
        required=["phone"],
    )
    def lookup_subscriber(phone: str) -> dict:
        return {"phone": phone}

    tools = build_tool_catalog(contract(), _Task())
    assert run(tools, "lookup_subscriber", phone="+33", urgency="high")["phone"] == "+33"


def test_a_tool_declaring_a_required_field_it_never_defines_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires 'phone' but never declares it"):

        @realtime_tool(name="bad", description="Bad.", required=["phone"])
        def bad() -> dict:
            return {}


def test_a_tool_without_a_description_is_rejected() -> None:
    """The model picks a tool by its description; a blank one is unusable."""

    with pytest.raises(ValueError, match="needs a description"):

        @realtime_tool(name="nameless", description="   ")
        def nameless() -> dict:
            return {}


def test_contract_tools_with_no_implementation_are_reported() -> None:
    """A promised-but-missing tool is how an agent offers a checkout it cannot do."""

    spec = contract(allowed_tools=["send_checkout_link", "callback_schedule"])
    tools = build_tool_catalog(spec, _Task())
    assert unimplemented_tools(spec, tools) == ["send_checkout_link"]


def test_deliberately_withheld_tools_are_not_reported_as_missing() -> None:
    """Retrieval is withheld when facts are inlined; that is a choice, not a gap."""

    spec = contract(
        allowed_tools=["knowledge_base_search", "lead_capture", "subscription_plan_lookup"]
    )
    tools = build_tool_catalog(spec, _Task())
    assert unimplemented_tools(spec, tools) == []


def test_a_broken_tool_file_is_reported_and_never_stops_a_call(tmp_path) -> None:
    (tmp_path / "broken.py").write_text("import nonexistent_module_xyz\n")
    (tmp_path / "good.py").write_text(
        "from phone_agent_gateway.ai_bridge.tasks.tool_registry import realtime_tool\n"
        "@realtime_tool(name='lookup_subscriber', description='Loaded from disk.')\n"
        "def lookup_subscriber():\n    return {'found': True}\n"
    )

    statuses = load_user_tools(tmp_path)

    assert statuses["good.py"] == "loaded"
    assert statuses["broken.py"].startswith("failed:")
    assert "lookup_subscriber" in registered_tools()


def test_an_edited_tool_file_is_reloaded_without_a_restart(tmp_path) -> None:
    path = tmp_path / "t.py"
    path.write_text(
        "from phone_agent_gateway.ai_bridge.tasks.tool_registry import realtime_tool\n"
        "@realtime_tool(name='lookup_subscriber', description='v1')\n"
        "def lookup_subscriber():\n    return {'version': 1}\n"
    )
    assert load_user_tools(tmp_path)["t.py"] == "loaded"
    assert load_user_tools(tmp_path)["t.py"] == "unchanged"

    path.write_text(
        "from phone_agent_gateway.ai_bridge.tasks.tool_registry import realtime_tool\n"
        "@realtime_tool(name='lookup_subscriber', description='v2')\n"
        "def lookup_subscriber():\n    return {'version': 2}\n"
    )
    import os

    os.utime(path, (0, 0))
    assert load_user_tools(tmp_path)["t.py"] == "loaded"
    assert registered_tools()["lookup_subscriber"].description == "v2"


def test_timeout_is_capped_so_a_tool_cannot_hold_the_line_open() -> None:
    @realtime_tool(name="lookup_subscriber", description="Greedy.", timeout_secs=600)
    def lookup_subscriber() -> dict:
        return {}

    assert registered_tools()["lookup_subscriber"].timeout_secs <= 10.0


def test_a_non_dict_return_is_still_speakable() -> None:
    async def handler() -> str:
        return "just a string"

    spec = ToolSpec(name="s", description="d", handler=handler)
    assert asyncio.run(run_tool(spec, {})) == "just a string"
