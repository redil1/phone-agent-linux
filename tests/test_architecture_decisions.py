"""Accepted platform decisions must be complete, indexed, and traceable."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADR_ROOT = ROOT / "docs" / "adr"
INDEX = ADR_ROOT / "README.md"
REQUIRED_HEADINGS = {
    "Context",
    "Decision",
    "Invariants",
    "Alternatives considered",
    "Consequences",
    "Migration and rollback",
    "Verification",
    "Supersession",
}


def test_nine_initial_architecture_decisions_are_indexed() -> None:
    records = sorted(ADR_ROOT.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert len(records) == 9
    index = INDEX.read_text(encoding="utf-8")
    assert "Accepted — implementation in transition" in index
    for expected_id, record in enumerate(records, start=1):
        assert record.name.startswith(f"{expected_id:04d}-")
        assert f"]({record.name})" in index


def test_each_decision_has_complete_governance_sections_and_evidence() -> None:
    for record in ADR_ROOT.glob("[0-9][0-9][0-9][0-9]-*.md"):
        source = record.read_text(encoding="utf-8")
        headings = set(re.findall(r"^## (.+)$", source, flags=re.MULTILINE))
        assert REQUIRED_HEADINGS <= headings, record.name
        assert "Status: Accepted" in source
        assert "Date: 2026-09-02" in source
        assert "Current conformance:" in source
        assert "docs/UNIVERSAL_CASCADE_PLATFORM_BACKLOG.md" in source
        assert re.search(r"M[0-9]+-[0-9]+", source), record.name


def test_cascade_decision_names_transition_debt_without_permitting_it() -> None:
    cascade = (ADR_ROOT / "0001-cascade-only-voice-runtime.md").read_text(encoding="utf-8")
    assert "S2S is forbidden production architecture" in cascade
    assert "ai_bridge/chatgpt_realtime_pipeline.py" in cascade
    assert "Rollback never means re-enabling S2S" in cascade
