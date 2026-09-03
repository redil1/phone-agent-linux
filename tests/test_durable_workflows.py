"""Tests for Durable Workflows and Human Collaboration (Milestone 12)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.durable_workflows import DurableWorkflowEngine


def test_durable_workflow_lifecycle_and_human_handoff() -> None:
    engine = DurableWorkflowEngine()

    wf_id = engine.start_workflow(
        workflow_name="lead_qualification",
        call_id="call_999",
        context={"caller_name": "Alice", "budget": 1000},
    )
    status = engine.get_status(wf_id)
    assert status.status == "running"

    # Human handoff
    engine.initiate_human_handoff(
        wf_id,
        summary="Caller requested custom enterprise SLA that requires VP approval.",
    )
    status2 = engine.get_status(wf_id)
    assert status2.status == "escalated_to_human"
    assert "enterprise SLA" in status2.summary_for_human

    # Complete
    engine.complete_workflow(wf_id, {"contract_signed": True})
    status3 = engine.get_status(wf_id)
    assert status3.status == "completed"
    assert status3.context["contract_signed"] is True
