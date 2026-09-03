"""Tests for Universal Tools, Policy, and Human Approval (Milestone 9)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.universal_tools import (
    ToolDefinition,
    UniversalToolPlane,
)


def test_tool_authorization_and_approval_workflow() -> None:
    plane = UniversalToolPlane()
    plane.register_tool(ToolDefinition(
        name="query_calendar",
        risk_level="read_only",
        requires_approval=False,
    ))
    plane.register_tool(ToolDefinition(
        name="charge_customer",
        risk_level="financial",
        requires_approval=True,
    ))

    # Read-only tool executes immediately
    res1 = plane.authorize_and_dispatch("query_calendar", {"date": "2026-09-04"}, call_id="c1")
    assert res1["status"] == "executed"

    # Financial action pauses for approval
    res2 = plane.authorize_and_dispatch("charge_customer", {"amount": 50}, call_id="c1")
    assert res2["status"] == "pending_approval"
    approval_id = res2["approval_id"]

    # Approve action
    decision = plane.decide_approval(approval_id, approved=True)
    assert decision["status"] == "approved"


def test_idempotency_enforcement() -> None:
    plane = UniversalToolPlane()
    plane.register_tool(ToolDefinition(
        name="send_sms",
        risk_level="reversible_write",
        requires_approval=False,
    ))
    res1 = plane.authorize_and_dispatch("send_sms", {"msg": "hi"}, call_id="c1", idempotency_key="key_123")
    assert res1["status"] == "executed"

    # Immediate replay is cached/ignored
    res2 = plane.authorize_and_dispatch("send_sms", {"msg": "hi"}, call_id="c1", idempotency_key="key_123")
    assert res2["status"] == "cached"
