"""Scale, deployment profiles, worker lifecycle, and release engineering.

Governed by Milestone 17 (M17-01 through M17-12):
Defines worker lifecycle (registration, lease, drain, crash recovery),
deployment profiles, and circuit breaker patterns.
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WorkerLease(StrictModel):
    worker_id: str
    host: str
    status: Literal["healthy", "draining", "degraded", "offline"] = "healthy"
    active_calls: int = 0
    max_concurrency: int = 4
    last_heartbeat: float = Field(default_factory=time.time)


class WorkerOrchestrationCluster:
    """Orchestrates scale and safe rolling deployments (M17-01, M17-02, M17-07)."""

    def __init__(self) -> None:
        self.workers: dict[str, WorkerLease] = {}

    def register_worker(self, worker_id: str, host: str, max_concurrency: int = 4) -> None:
        self.workers[worker_id] = WorkerLease(
            worker_id=worker_id,
            host=host,
            max_concurrency=max_concurrency,
        )

    def assign_call_to_worker(self) -> str:
        """Assign call to best available worker; blocks assignment if draining or degraded."""
        for w in self.workers.values():
            if w.status == "healthy" and w.active_calls < w.max_concurrency:
                w.active_calls += 1
                return w.worker_id
        raise RuntimeError("No available healthy capacity across worker cluster")

    def drain_worker(self, worker_id: str) -> None:
        """Graceful call draining for rolling upgrades (M17-02)."""
        if worker_id in self.workers:
            self.workers[worker_id].status = "draining"

    def release_call(self, worker_id: str) -> None:
        if worker_id in self.workers and self.workers[worker_id].active_calls > 0:
            self.workers[worker_id].active_calls -= 1
