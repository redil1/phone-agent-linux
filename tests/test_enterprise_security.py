"""Tests for Multi-Tenancy and Enterprise Security (Milestone 16)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.enterprise_security import (
    EnterpriseSecurityManager,
    TenantContext,
)


def test_rbac_authorization_and_audit_hash_chain() -> None:
    sec = EnterpriseSecurityManager()

    admin_ctx = TenantContext(tenant_id="t1", organization_name="Acme", role="admin")
    op_ctx = TenantContext(tenant_id="t1", organization_name="Acme", role="operator")
    aud_ctx = TenantContext(tenant_id="t2", organization_name="Globex", role="auditor")

    # Admin can perform anything
    assert sec.authorize(admin_ctx, "delete_agent") is True
    # Operator cannot delete agent
    assert sec.authorize(op_ctx, "delete_agent") is False
    # Auditor cannot deploy package
    assert sec.authorize(aud_ctx, "deploy_package") is False
    # Auditor can view audit
    assert sec.authorize(aud_ctx, "view_audit") is True

    # Validate cryptographic audit chaining
    chain = sec.audit_chain
    assert len(chain) == 4
    assert chain[0].previous_hash == "0" * 64
    assert chain[1].previous_hash == chain[0].entry_hash
    assert chain[2].previous_hash == chain[1].entry_hash
    assert chain[3].previous_hash == chain[2].entry_hash
