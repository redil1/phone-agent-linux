"""Sales intelligence must never become a source of spoken facts."""

import pytest
from src.extractor.sales_intelligence import build_prompt, normalize


def test_market_context_is_marked_as_unquotable():
    prompt = build_prompt("OWN SITE TEXT", "COMPETITOR TEXT", "Streamly")
    assert "never quote, never name a competitor" in prompt
    assert "the only source you may quote facts from" in prompt


def test_absent_market_context_falls_back_to_category_knowledge():
    prompt = build_prompt("OWN SITE TEXT", "", "Streamly")
    assert "none gathered" in prompt
    assert "your own knowledge of this" in prompt


def test_oversized_sources_are_truncated_not_refused():
    prompt = build_prompt("x" * 200_000, "y" * 200_000, "Streamly")
    assert len(prompt) < 70_000


def test_a_malformed_reply_degrades_to_empty_rather_than_raising():
    result = normalize({"objections": "not a list", "sample_phrases": None, "buyer": 7})
    assert result["objections"] == []
    assert result["sample_phrases"] == {}
    assert result["buyer"]["who"] == ""


def test_incomplete_objections_are_dropped():
    result = normalize({"objections": [
        {"objection": "Too costly", "answer": "It pays back in a month."},
        {"objection": "No answer given"},
        {"answer": "No objection given"},
        "not an object",
    ]})
    assert [o["objection"] for o in result["objections"]] == ["Too costly"]


def test_output_is_bounded():
    result = normalize({
        "objections": [{"objection": f"o{i}", "answer": "a"} for i in range(50)],
        "sample_phrases": {f"p{i}": "x" for i in range(50)},
        "discovery_questions": [f"q{i}" for i in range(50)],
        "vocabulary": [f"v{i}" for i in range(50)],
    })
    assert len(result["objections"]) <= 12
    assert len(result["sample_phrases"]) <= 12
    assert len(result["discovery_questions"]) <= 5
    assert len(result["vocabulary"]) <= 10


@pytest.mark.parametrize("source,kept", [("faq", True), ("market", True), ("invented", False)])
def test_only_known_sources_are_recorded(source: str, kept: bool):
    result = normalize({"objections": [
        {"objection": "o", "answer": "a", "source": source}]})
    assert ("source" in result["objections"][0]) is kept
