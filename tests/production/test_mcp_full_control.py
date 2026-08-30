"""An external agent must be able to run this appliance, not merely dial it."""

from __future__ import annotations

from typing import Any

import pytest

from phone_agent_gateway.ai_bridge import mcp_server
from phone_agent_gateway.ai_bridge.local_control import LocalControlError


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    seen: list[tuple[str, str, Any]] = []

    def fake(method: str, path: str, *, payload: Any = None, **_: Any) -> dict[str, Any]:
        seen.append((method, path, payload))
        return {"status": "ok"}

    monkeypatch.setattr(mcp_server, "local_control_request", fake)
    return seen


def test_every_declared_tool_can_be_dispatched() -> None:
    """A tool the model can see but not call is worse than one that is absent."""

    body = open(mcp_server.__file__).read().split("def _call_tool")[1]
    missing = [t["name"] for t in mcp_server.TOOLS if f'"{t["name"]}"' not in body]

    assert missing == []


def test_the_full_configuration_surface_is_reachable(calls) -> None:
    """Choosing the model, the tools, the persona and the task is the appliance."""

    mcp_server._call_tool("phone_agent_get_configuration", {})
    mcp_server._call_tool("phone_agent_set_configuration", {"llm_provider": "ollama"})
    mcp_server._call_tool("phone_agent_get_tool_control", {})
    mcp_server._call_tool("phone_agent_set_tool_control", {"config": {}})
    mcp_server._call_tool("phone_agent_get_persona", {})
    mcp_server._call_tool("phone_agent_set_persona", {"identity": {}})
    mcp_server._call_tool("phone_agent_set_task", {"id": "t"})
    mcp_server._call_tool("phone_agent_delete_task", {"task_id": "t"})

    paths = [path for _, path, _ in calls]
    assert paths == [
        "/api/config",
        "/api/config",
        "/api/tools",
        "/api/tools",
        "/api/persona",
        "/api/persona",
        "/api/tasks",
        "/api/tasks/delete",
    ]


@pytest.mark.parametrize("integration", ["frappe", "openwa", "web-research"])
def test_each_business_integration_is_reachable(integration: str, calls) -> None:
    mcp_server._call_tool("phone_agent_get_integration", {"integration": integration})
    mcp_server._call_tool(
        "phone_agent_set_integration", {"integration": integration, "config": {"a": 1}}
    )
    mcp_server._call_tool("phone_agent_test_integration", {"integration": integration})

    assert [path for _, path, _ in calls] == [
        f"/api/{integration}",
        f"/api/{integration}",
        f"/api/{integration}/test",
    ]


def test_an_unknown_integration_cannot_reach_an_arbitrary_endpoint(calls) -> None:
    """The name goes into the URL, so it must never be free text."""

    with pytest.raises(LocalControlError, match="unknown integration"):
        mcp_server._call_tool(
            "phone_agent_get_integration", {"integration": "../control/dial"}
        )

    assert calls == []


def test_the_handset_link_is_controllable(calls) -> None:
    mcp_server._call_tool("phone_agent_set_remote_link", {"enabled": True, "port": 8770})
    mcp_server._call_tool("phone_agent_pairing_code", {"rotate": False})

    assert [path for _, path, _ in calls] == ["/api/remote-link", "/api/pairing"]


def test_approvals_can_be_read_and_decided(calls) -> None:
    mcp_server._call_tool("phone_agent_list_approvals", {})
    mcp_server._call_tool(
        "phone_agent_decide_approval", {"request_id": "r1", "approved": True}
    )

    assert [path for _, path, _ in calls] == ["/api/approvals", "/api/approvals/decide"]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("phone_agent_set_configuration", {}),
        ("phone_agent_set_tool_control", {"config": "not an object"}),
        ("phone_agent_delete_task", {}),
        ("phone_agent_delete_task", {"task_id": 5}),
        ("phone_agent_set_remote_link", {"enabled": "yes"}),
        ("phone_agent_decide_approval", {"request_id": "r1"}),
        ("phone_agent_set_integration", {"integration": "frappe"}),
        ("phone_agent_get_persona", {"unexpected": 1}),
    ],
)
def test_malformed_arguments_never_reach_studio(name, arguments, calls) -> None:
    """Validation happens before the request, so a bad call changes nothing."""

    with pytest.raises(LocalControlError):
        mcp_server._call_tool(name, arguments)

    assert calls == []


def test_dialling_still_requires_a_human(calls) -> None:
    """Config control is broad on purpose; calling a real person is not."""

    mcp_server._call_tool(
        "phone_agent_request_dial",
        {"destination": "+212600000000", "recording_consent": False},
    )

    assert calls[0][1] == "/api/mcp/dial/request"  # a request, never a call
