"""Tests for Knowledge Ingestion and Grounded Retrieval (Milestone 10)."""

from __future__ import annotations

from phone_agent_gateway.ai_bridge.knowledge_retrieval import (
    GroundedFact,
    KnowledgeRetrievalEngine,
)


def test_tenant_isolated_retrieval_and_expired_fact_suppression() -> None:
    engine = KnowledgeRetrievalEngine()

    # Ingest tenant A facts
    engine.ingest_fact(
        "tenant_a",
        GroundedFact(
            fact_id="f1",
            tenant_id="tenant_a",
            topic="pricing",
            content="Standard tier is $25 per month.",
            is_active=True,
        ),
    )
    # Ingest deactivated fact
    engine.ingest_fact(
        "tenant_a",
        GroundedFact(
            fact_id="f2",
            tenant_id="tenant_a",
            topic="pricing",
            content="Old promotion is $10 per month.",
            is_active=False,
        ),
    )
    # Ingest tenant B fact
    engine.ingest_fact(
        "tenant_b",
        GroundedFact(
            fact_id="f3",
            tenant_id="tenant_b",
            topic="pricing",
            content="Enterprise tier is $100 per month.",
            is_active=True,
        ),
    )

    # Query tenant A
    res_a = engine.retrieve("tenant_a", "pricing standard tier")
    assert len(res_a) == 1
    assert "Standard tier is $25 per month." in res_a[0]

    # Confirm deactivated fact is never retrieved
    res_a_old = engine.retrieve("tenant_a", "promotion")
    assert len(res_a_old) == 0

    # Confirm strict tenant isolation: Tenant A cannot retrieve Tenant B facts
    res_cross = engine.retrieve("tenant_a", "Enterprise")
    assert len(res_cross) == 0
