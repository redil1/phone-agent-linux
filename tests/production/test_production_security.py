from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from phone_agent_gateway.ai_bridge.production_security import (
    AuditLedger,
    CallPolicy,
    PolicyConfig,
    PolicyError,
    load_policy,
    normalize_destination,
)


def test_destination_normalization_and_emergency_block() -> None:
    assert normalize_destination("00212600454425") == "+212600454425"
    assert normalize_destination("0600454425") == "+212600454425"
    policy = CallPolicy(PolicyConfig(), salt="test")
    assert not policy.decide_dial("112", approved=True).allowed


def test_policy_requires_approval_cooldown_and_rate() -> None:
    policy = CallPolicy(
        PolicyConfig(max_calls_per_hour=2, destination_cooldown_secs=10), salt="test"
    )
    assert not policy.decide_dial("+212600454425", approved=False, now=100).allowed
    assert policy.decide_dial("+212600454425", approved=True, now=100).allowed
    assert not policy.decide_dial("+212600454425", approved=True, now=101).allowed
    assert policy.decide_dial("+212600000000", approved=True, now=102).allowed
    assert not policy.decide_dial("+212611111111", approved=True, now=103).allowed


def test_policy_preview_does_not_consume_rate_or_cooldown() -> None:
    policy = CallPolicy(
        PolicyConfig(max_calls_per_hour=1, destination_cooldown_secs=300), salt="test"
    )
    assert policy.decide_dial("+212600454425", approved=True, now=100, reserve=False).allowed
    assert policy.decide_dial("+212600454425", approved=True, now=101).allowed


def test_policy_file_is_exact_and_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 1, "dial_enabled": False}))
    path.chmod(0o600)
    assert load_policy(path).dial_enabled is False
    path.write_text(json.dumps({"version": 1, "unknown": True}))
    path.chmod(0o600)
    with pytest.raises(PolicyError):
        load_policy(path)
    path.chmod(0o666)
    with pytest.raises(PolicyError, match="group/world writable"):
        load_policy(path)


def test_audit_ledger_is_chained_private_and_durable(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path)
    first = ledger.append("dial_allowed", {"destination": "sha256:test:last4:4425"})
    second = ledger.append("call_ended", {"outcome": "completed"})
    assert second["previous_hash"] == first["hash"]
    assert os.stat(path).st_mode & 0o777 == 0o600
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[1]["hash"] == second["hash"]
