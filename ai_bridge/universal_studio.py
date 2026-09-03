"""Universal Agent Studio operations and deployment manager.

Governed by Milestone 15 (M15-01 through M15-12):
Defines studio workspace, package inspection, live call state operations,
and authoritative worker synchronization without stale state representation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StudioLiveCallView(StrictModel):
    call_id: str
    channel: str
    stage: str
    latency_p50_ms: float
    active_agent_id: str


class UniversalStudioManager:
    """Universal Studio lifecycle manager (M15-01, M15-10, M15-11)."""

    def __init__(self) -> None:
        self.active_packages: dict[str, Any] = {}
        self.active_calls: dict[str, StudioLiveCallView] = {}

    def deploy_package(self, package_id: str, bundle: Any) -> None:
        self.active_packages[package_id] = bundle

    def register_call(self, call_view: StudioLiveCallView) -> None:
        self.active_calls[call_view.call_id] = call_view

    def get_live_overview(self) -> dict[str, Any]:
        return {
            "total_active_calls": len(self.active_calls),
            "deployed_packages": list(self.active_packages.keys()),
            "calls": [c.model_dump() for c in self.active_calls.values()],
        }
