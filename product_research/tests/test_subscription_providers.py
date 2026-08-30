"""Subscription-backed extraction must fail loudly, never silently."""

import pytest
from src.extractor.subscription_providers import _parse_json


def test_a_fenced_json_reply_is_unwrapped():
    parsed = _parse_json('```json\n{"plans": [{"name": "Starter"}]}\n```', "Codex")
    assert parsed["plans"][0]["name"] == "Starter"


def test_prose_around_the_object_is_discarded():
    parsed = _parse_json('Here you go:\n{"ok": true}\nHope that helps.', "Codex")
    assert parsed == {"ok": True}


def test_unparseable_output_names_the_provider_and_shows_the_start():
    with pytest.raises(RuntimeError, match="Codex did not return parseable JSON"):
        _parse_json("I cannot help with that request.", "Codex")


def test_a_json_array_is_rejected_as_the_wrong_shape():
    with pytest.raises(RuntimeError, match="expected a JSON object"):
        _parse_json("[1, 2, 3]", "Antigravity")
