"""The normalizer must survive whatever a model actually returns.

A smaller local model omits entire sub-objects. `raw.get(key, {})` builds a
fresh dict, so writing back through `parent[key]` raised KeyError and killed a
research run several minutes after the crawl had already finished.
"""

import pytest
from src.extractor.extractor import normalize_llm_json
from src.schemas.product_schema import ProductKnowledgeBase


def validate(raw: dict, name: str = "IPTV Shopping", url: str = "https://iptv.shopping"):
    """Mirror what ProductExtractor.extract_from_markdown does around the normalizer."""

    raw = dict(raw)
    raw.setdefault("product_name", name)
    raw.setdefault("company_name", name)
    raw.setdefault("website_url", url)
    raw.setdefault("tagline", f"The official platform for {name}")
    return ProductKnowledgeBase.model_validate(normalize_llm_json(raw, name, url))


@pytest.mark.parametrize(
    "label,raw",
    [
        ("completely empty", {}),
        (
            "every pillar present but empty",
            {
                "core_specs": {},
                "commercials_pricing": {},
                "value_prop_roi": {},
                "competitive_intel": {},
                "implementation_support": {},
                "security_compliance": {},
                "guardrails_disqualifiers": {},
            },
        ),
        ("integrations omitted", {"core_specs": {"summary": "x", "features": ["Live TV"]}}),
        ("release_info omitted", {"core_specs": {"summary": "x", "integrations": {}}}),
        (
            "displacement_strategy omitted",
            {"competitive_intel": {"battlecards": []}},
        ),
        ("pillar-numbered keys", {"pillar_1_core_specs": {"summary": "x"}}),
        ("features given as plain strings", {"core_specs": {"features": ["Live TV", "VOD"]}}),
        ("integrations given as a list", {"core_specs": {"integrations": ["Firestick"]}}),
        ("release_info given as a string", {"core_specs": {"release_info": "shipping now"}}),
    ],
)
def test_a_sparse_model_response_still_normalizes(label: str, raw: dict) -> None:
    knowledge_base = validate(raw)
    assert knowledge_base.product_name == "IPTV Shopping"


def test_defaults_are_filled_when_a_sub_object_is_missing() -> None:
    knowledge_base = validate({"core_specs": {"summary": "A streaming service."}})
    assert knowledge_base.core_specs.integrations.api_capabilities
    assert knowledge_base.core_specs.release_info.current_version


def test_supplied_values_are_kept_not_overwritten() -> None:
    knowledge_base = validate(
        {
            "core_specs": {
                "summary": "A streaming service.",
                "integrations": {"native_integrations": ["Firestick", "Apple TV"]},
            }
        }
    )
    assert "Firestick" in knowledge_base.core_specs.integrations.native_integrations
