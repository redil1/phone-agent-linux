from __future__ import annotations

import hashlib
import json
import re
import wave
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = ROOT / "qualification" / "corpus" / "v1"
MANIFEST_PATH = CORPUS_ROOT / "manifest.json"
REQUIRED_COVERAGE = {
    "clear_speech",
    "noise",
    "interruption",
    "silence",
    "fragment",
    "english",
    "french",
    "tool_call",
    "long_turn",
    "call_teardown",
}
CUSTOMER_IDENTIFIER = re.compile(r"(?:\+?\d[\s().-]*){8,}")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_protected_baseline_corpus_is_complete_and_integrity_checked() -> None:
    manifest = _load_json(MANIFEST_PATH)

    assert manifest["schema_version"] == 1
    assert manifest["corpus_id"] == "phoneagent-cascade-baseline-v1"
    assert manifest["provenance"]["synthetic_only"] is True
    assert manifest["provenance"]["contains_customer_data"] is False
    assert set(manifest["coverage"]) == REQUIRED_COVERAGE

    scenarios = manifest["scenarios"]
    assert len(scenarios) >= 9
    assert {category for item in scenarios for category in item["categories"]} == REQUIRED_COVERAGE

    for scenario in scenarios:
        assert scenario["id"]
        assert scenario["language"] in {"en-US", "fr-FR", "und"}
        for artifact_name in ("audio", "transcript", "events"):
            artifact = scenario[artifact_name]
            path = CORPUS_ROOT / artifact["path"]
            assert path.is_file(), f"missing {artifact_name} for {scenario['id']}"
            assert _sha256(path) == artifact["sha256"]

        audio_path = CORPUS_ROOT / scenario["audio"]["path"]
        with wave.open(str(audio_path), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() == 16_000
            assert wav.getnframes() > 0

        transcript = _load_json(CORPUS_ROOT / scenario["transcript"]["path"])
        events = _load_json(CORPUS_ROOT / scenario["events"]["path"])
        assert transcript["scenario_id"] == scenario["id"]
        assert transcript["provenance"] == "synthetic"
        assert events["scenario_id"] == scenario["id"]
        assert events["events"]

        serialized = json.dumps({"transcript": transcript, "events": events})
        assert not CUSTOMER_IDENTIFIER.search(serialized)


def test_baseline_corpus_manifest_contains_no_unlisted_artifacts() -> None:
    manifest = _load_json(MANIFEST_PATH)
    listed = {"manifest.json"}
    for scenario in manifest["scenarios"]:
        listed.update(scenario[name]["path"] for name in ("audio", "transcript", "events"))

    actual = {
        str(path.relative_to(CORPUS_ROOT))
        for path in CORPUS_ROOT.rglob("*")
        if path.is_file()
    }
    assert actual == listed
