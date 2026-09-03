"""Commercial readiness, licensing, marketplace governance, and tenancy onboarding.

Governed by Milestone 18 (M18-09, M18-10) and Milestone 19 (M19-01 through M19-10):
Defines editions, metering, billing counters, and publisher verification.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MeteringRecord(StrictModel):
    tenant_id: str
    call_minutes: float = 0.0
    tokens_consumed: int = 0
    tools_dispatched: int = 0
    edition: Literal["appliance", "on_prem", "managed_cloud"] = "managed_cloud"


class CommercialReadinessManager:
    """Manages metering, billing aggregation, and tenant onboarding (M19-01, M19-02, M19-03)."""

    def __init__(self) -> None:
        self.usage: dict[str, MeteringRecord] = {}

    def record_usage(self, tenant_id: str, minutes: float, tokens: int, tools: int) -> None:
        if tenant_id not in self.usage:
            self.usage[tenant_id] = MeteringRecord(tenant_id=tenant_id)
        u = self.usage[tenant_id]
        u.call_minutes += minutes
        u.tokens_consumed += tokens
        u.tools_dispatched += tools

    def get_billable_usage(self, tenant_id: str) -> MeteringRecord:
        return self.usage.get(tenant_id, MeteringRecord(tenant_id=tenant_id))
