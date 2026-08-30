"""Shared test setup.

The tool registry imports Python files from the operator's real
``~/.config/phone-agent/tools/`` directory. Without this the suite would pass or
fail depending on which tools a given developer happens to have installed, so
every test runs against an empty tools directory unless it says otherwise.
"""

from __future__ import annotations

import pytest

from phone_agent_gateway.ai_bridge import openwa_integration, tool_control, web_research
from phone_agent_gateway.ai_bridge.tasks import tool_registry


@pytest.fixture(autouse=True)
def _isolated_user_tools(tmp_path_factory, monkeypatch):
    empty = tmp_path_factory.mktemp("no-user-tools")
    managed = tmp_path_factory.mktemp("no-managed-tools")
    monkeypatch.setattr(tool_registry, "USER_TOOLS_DIR", empty)
    monkeypatch.setattr(tool_control, "DEFAULT_TOOL_CONTROL_PATH", managed / "tools.json")
    monkeypatch.setattr(tool_control, "DEFAULT_APPROVAL_DIR", managed / "approvals")
    monkeypatch.setattr(
        openwa_integration,
        "DEFAULT_OPENWA_CONFIG_PATH",
        managed / "openwa.json",
    )
    monkeypatch.setattr(
        web_research,
        "DEFAULT_WEB_RESEARCH_CONFIG_PATH",
        managed / "web-research.json",
    )
    monkeypatch.delenv("PHONE_AGENT_TOOL_CONTROL", raising=False)
    monkeypatch.delenv("PHONE_AGENT_TOOL_APPROVAL_DIR", raising=False)
    monkeypatch.delenv("PHONE_AGENT_OPENWA_CONFIG", raising=False)
    monkeypatch.delenv("PHONE_AGENT_WEB_RESEARCH_CONFIG", raising=False)
    # Constructing the Studio starts a warm voice host in production so the first
    # dial does not pay the ~20 s speech-model load. That spawns a real
    # subprocess, so the suite runs with it off.
    monkeypatch.setenv("PHONE_AGENT_WARM_VOICE_HOST", "0")
    tool_registry.clear_registry()
    yield
    tool_registry.clear_registry()
