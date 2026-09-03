from __future__ import annotations

from pathlib import Path

from phone_agent_gateway.ci.validate_s2s_inventory import discover_surface, validate_inventory

ROOT = Path(__file__).resolve().parents[1]


def test_s2s_surface_is_complete_classified_and_migration_owned() -> None:
    result = validate_inventory(ROOT)

    assert result["status"] == "pass"
    assert result["surface_count"] == 96
    assert result["group_count"] == 11
    assert result["configuration_key_count"] == 18
    assert result["dependency_binding_count"] == 4
    assert result["persisted_surface_count"] == 5
    assert result["runtime_branch_count"] == 9
    assert result["event_contract_count"] == 2
    assert result["event_name_count"] == 47
    assert result["shared_behavior_count"] == 10
    assert result["disposition_counts"] == {
        "delete": 52,
        "migrate": 21,
        "retain_historical": 9,
        "rewrite": 14,
    }


def test_detector_finds_a_new_hidden_execution_surface(tmp_path: Path) -> None:
    hidden = tmp_path / "adapter.py"
    hidden.write_text("async def speech_to_speech_runtime(): pass\n", encoding="utf-8")

    assert discover_surface(tmp_path) == {"adapter.py"}
