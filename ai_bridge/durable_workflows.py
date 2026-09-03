"""Durable workflows and human collaboration engine.

Governed by Milestone 12 (M12-01 through M12-08):
Defines workflow boundary, correlation tracking, asynchronous activity execution,
and warm human handoff protocol.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WorkflowExecutionRecord(StrictModel):
    workflow_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_name: str
    call_id: str
    status: Literal["running", "completed", "escalated_to_human", "failed"] = "running"
    context: dict[str, Any] = Field(default_factory=dict)
    summary_for_human: str = Field(default="")


class DurableWorkflowEngine:
    """Manages asynchronous business workflows outside live audio turns (M12-01, M12-05)."""

    def __init__(self) -> None:
        self.executions: dict[str, WorkflowExecutionRecord] = {}

    def start_workflow(self, workflow_name: str, call_id: str, context: dict[str, Any]) -> str:
        rec = WorkflowExecutionRecord(
            workflow_name=workflow_name,
            call_id=call_id,
            context=context,
        )
        self.executions[rec.workflow_id] = rec
        return rec.workflow_id

    def complete_workflow(self, workflow_id: str, results: dict[str, Any]) -> None:
        if workflow_id not in self.executions:
            raise KeyError(f"Unknown workflow {workflow_id}")
        self.executions[workflow_id].status = "completed"
        self.executions[workflow_id].context.update(results)

    def initiate_human_handoff(self, workflow_id: str, summary: str) -> None:
        """Warm human transfer protocol with structured context (M12-05)."""
        if workflow_id not in self.executions:
            raise KeyError(f"Unknown workflow {workflow_id}")
        self.executions[workflow_id].status = "escalated_to_human"
        self.executions[workflow_id].summary_for_human = summary

    def get_status(self, workflow_id: str) -> WorkflowExecutionRecord:
        return self.executions[workflow_id]
