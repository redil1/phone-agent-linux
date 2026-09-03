"""Multi-tenancy and enterprise security controls.

Governed by Milestone 16 (M16-01 through M16-10):
Defines tenancy model, role-based access controls (RBAC),
and immutable audit log with cryptographic integrity chaining.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TenantContext(StrictModel):
    tenant_id: str
    organization_name: str
    role: Literal["admin", "operator", "auditor", "caller"]


class SecurityAuditLogEntry(StrictModel):
    sequence_id: int
    tenant_id: str
    actor: str
    action: str
    decision: Literal["allow", "deny"]
    previous_hash: str
    entry_hash: str


class EnterpriseSecurityManager:
    """Enterprise security and RBAC manager (M16-01, M16-04, M16-08)."""

    def __init__(self) -> None:
        self.audit_chain: list[SecurityAuditLogEntry] = []
        self._last_hash = "0" * 64

    def authorize(self, context: TenantContext, action: str) -> bool:
        """Role-Based Access Control."""
        allowed = False
        if context.role == "admin":
            allowed = True
        elif context.role == "operator" and action in {"view_calls", "deploy_package", "approve_tool"}:
            allowed = True
        elif context.role == "auditor" and action in {"view_logs", "view_audit"}:
            allowed = True

        # Append to cryptographically chained audit log
        seq = len(self.audit_chain)
        decision: Literal["allow", "deny"] = "allow" if allowed else "deny"
        raw = f"{seq}:{context.tenant_id}:{context.actor_id if hasattr(context, 'actor_id') else 'user'}:{action}:{decision}:{self._last_hash}"
        entry_hash = hashlib.sha256(raw.encode()).hexdigest()

        entry = SecurityAuditLogEntry(
            sequence_id=seq,
            tenant_id=context.tenant_id,
            actor=context.role,
            action=action,
            decision=decision,
            previous_hash=self._last_hash,
            entry_hash=entry_hash,
        )
        self.audit_chain.append(entry)
        self._last_hash = entry_hash
        return allowed
