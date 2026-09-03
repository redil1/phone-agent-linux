"""The S2S deletion prerequisite must remain complete and executable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from phone_agent_gateway.ci.validate_cascade_characterization import (
    CharacterizationError,
    validate_characterization,
)

ROOT = Path(__file__).resolve().parents[1]


def test_all_shared_behaviors_have_cascade_success_and_failure_contracts() -> None:
    result = validate_characterization(ROOT)

    assert result["status"] == "pass"
    assert result["pipeline"] == "cascade"
    assert result["behavior_count"] == 10
    assert result["test_node_count"] >= 20


def test_characterization_rejects_a_missing_failure_regression(tmp_path: Path) -> None:
    source = ROOT / "migration" / "cascade-characterization-v1.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["contracts"][0]["failure_nodeids"] = []
    candidate = tmp_path / "incomplete.json"
    candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CharacterizationError, match="non-empty"):
        validate_characterization(ROOT, candidate)
