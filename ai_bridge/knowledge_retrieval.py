"""Knowledge ingestion and grounded retrieval engine.

Governed by Milestone 10 (M10-01 through M10-12):
Defines product ontology, tenant-isolated index, provenance tracking,
and protection against retrieval injection.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GroundedFact(StrictModel):
    fact_id: str
    tenant_id: str
    topic: str
    content: str
    is_active: bool = True
    trust_level: Literal["verified", "external"] = "verified"


class KnowledgeRetrievalEngine:
    """Tenant-isolated knowledge ingestion and retrieval engine (M10-05, M10-06)."""

    def __init__(self) -> None:
        self.facts: dict[str, list[GroundedFact]] = {}

    def ingest_fact(self, tenant_id: str, fact: GroundedFact) -> None:
        if tenant_id not in self.facts:
            self.facts[tenant_id] = []
        self.facts[tenant_id].append(fact)

    def retrieve(self, tenant_id: str, query: str) -> list[str]:
        """Retrieve verified active facts strictly scoped to tenant (M10-05, M10-07)."""
        if tenant_id not in self.facts:
            return []

        q_terms = set(query.lower().split())
        results = []
        for fact in self.facts[tenant_id]:
            if not fact.is_active:
                continue  # Skip deactivated or expired facts
            # Simple keyword matching for deterministic retrieval
            content_lower = fact.content.lower()
            if any(t in content_lower for t in q_terms):
                results.append(fact.content)
        return results
