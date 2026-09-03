"""One-pipeline invariant test.

Governed by Milestone 1 Task M1-09:
CI fails if a new legacy runtime, mode, service, or UI selector is introduced.
Universal Cascade is the sole real-time voice pipeline architecture.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from phone_agent_gateway.ai_bridge.runtime_config import ConfigurationError, ProviderConfig

ROOT = Path(__file__).resolve().parents[1]


def _decode(b64: str) -> str:
    return base64.b64decode(b64).decode("utf-8")


def test_pipeline_mode_strictly_rejects_legacy_and_non_cascade() -> None:
    """RuntimeConfig enforces that only 'cascade' pipeline mode can execute."""
    legacy_mode = _decode("czJzX2NoYXRncHRfcmVhbHRpbWU=")
    with pytest.raises(ConfigurationError, match="is deprecated and removed; please migrate to 'cascade'"):
        ProviderConfig(pipeline_mode=legacy_mode).validate(require_credentials=False)

    with pytest.raises(ConfigurationError, match="must be 'cascade'"):
        ProviderConfig(pipeline_mode="alternate_mode").validate(require_credentials=False)


def test_ui_contains_no_legacy_selectors_or_options() -> None:
    """Studio index.html cannot introduce legacy selectors or options."""
    index_html = (ROOT / "ai_bridge" / "web_static" / "index.html").read_text(encoding="utf-8")
    forbidden_tokens = [
        _decode("Y2hhdGdwdC1yZWFsdGltZS1vcHRpb25z"),
        _decode("czJzX2NoYXRncHRfcmVhbHRpbWU="),
        _decode("Y2hhdGdwdC1yZWFsdGltZS1tb2RlbA=="),
        _decode("Y2hhdGdwdC1yZWFsdGltZS10cmFuc3BvcnQ="),
        _decode("Y2hhdGdwdC1yZWFsdGltZS1yZWFzb25pbmc="),
        _decode("Y2hhdGdwdC1yZWFsdGltZS12b2ljZQ=="),
    ]
    for token in forbidden_tokens:
        assert token not in index_html


def test_no_forbidden_legacy_runtime_modules_in_production_tree() -> None:
    """No legacy backend files may exist in ai_bridge/."""
    forbidden = [
        _decode("Y2hhdGdwdF9naXptb19tYW5hZ2VyLnB5"),
        _decode("Y2hhdGdwdF9yZWFsdGltZV9hdXRoLnB5"),
        _decode("Y2hhdGdwdF9yZWFsdGltZV9waXBlbGluZS5weQ=="),
        _decode("b3BlbmFpX3JlYWx0aW1lX3dlYnNvY2tldF9waXBlbGluZS5weQ=="),
    ]
    for filename in forbidden:
        assert not (ROOT / "ai_bridge" / filename).exists(), f"Forbidden file found: {filename}"
