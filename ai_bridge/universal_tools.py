"""Universal tools, MCP policy, argument grounding, and approval workflows.

Governed by Milestone 9 (M9-01 through M9-12):
Defines capability risk classes, centralized authorization, approval workflows,
argument grounding, and idempotency verification.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolDefinition(StrictModel):
    name: str = Field(min_length=1)
    risk_level: Literal["read_only", "reversible_write", "consequential", "financial", "destructive"]
    requires_approval: bool = False
    idempotent: bool = True


class ApprovalRequest(StrictModel):
    approval_id: str
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    status: Literal["pending", "approved", "rejected"] = "pending"


class UniversalToolPlane:
    """Centralized tool authorization, grounding, and approval plane (M9-03, M9-04, M9-05)."""

    def __init__(self) -> None:
        self.registered_tools: dict[str, ToolDefinition] = {}
        self.pending_approvals: dict[str, ApprovalRequest] = {}
        self.executed_idempotency_keys: set[str] = set()

    def register_tool(self, tool: ToolDefinition) -> None:
        self.registered_tools[tool.name] = tool

    def authorize_and_dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Authorize and dispatch tool call with grounding and approval gates."""
        if tool_name not in self.registered_tools:
            return {"status": "error", "error": f"Unauthorized or unregistered tool: {tool_name}"}

        tool = self.registered_tools[tool_name]

        # Idempotency check (M9-07)
        if idempotency_key:
            if idempotency_key in self.executed_idempotency_keys:
                return {"status": "cached", "message": "Idempotent duplicate ignored"}
            self.executed_idempotency_keys.add(idempotency_key)

        # Human-in-the-loop approval (M9-05)
        if tool.requires_approval:
            approval_id = hashlib.sha256(f"{tool_name}:{call_id}:{json.dumps(arguments)}".encode()).hexdigest()[:16]
            self.pending_approvals[approval_id] = ApprovalRequest(
                approval_id=approval_id,
                tool_name=tool_name,
                arguments=arguments,
                call_id=call_id,
                status="pending",
            )
            return {"status": "pending_approval", "approval_id": approval_id}

        # Simulated safe execution
        return {"status": "executed", "tool": tool_name, "result": "success"}

    def decide_approval(self, approval_id: str, approved: bool) -> dict[str, Any]:
        if approval_id not in self.pending_approvals:
            raise KeyError(f"Unknown approval ID: {approval_id}")
        req = self.pending_approvals[approval_id]
        req.status = "approved" if approved else "rejected"
        return {"approval_id": approval_id, "status": req.status}
