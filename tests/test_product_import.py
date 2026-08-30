"""An imported product may activate itself only if every claim is traceable."""

from __future__ import annotations

import json

import pytest

from phone_agent_gateway.ai_bridge.tasks.product_import import (
    _subject_terms,
    build_contract,
    candidate_facts,
    import_product,
    verify_fact,
)
from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

# Padded with filler so the 400-character proximity window is meaningful; in a
# short document every claim sits next to every other one.
FILLER = ("Streamly brings your favourite programmes together in one place. " * 12) + "\n"

SOURCE = (
    FILLER
    + "# Pricing\n"
    + "The Starter plan is $25 for three months and includes one active connection.\n"
    + FILLER
    + "The Advanced plan is $59 for 12 months, our best value.\n"
    + FILLER
    + "Volume discounts of up to 15% are available on committed annual contracts.\n"
    + FILLER
    + "We are SOC2 and HIPAA certified, with 99.9% uptime for enterprise customers.\n"
    + "Setup takes about 10 minutes.\n"
    + FILLER
    + "Blog archive: May 15, 2025 promotions roundup.\n"
    + FILLER
)


def kb(**overrides) -> dict:
    base = {
        "product_name": "Streamly",
        "company_name": "Streamly Media",
        "tagline": "Watch everything",
        "core_specs": {
            "summary": "Streamly is a streaming platform.",
            "features": [
                {"name": "One active connection", "description": "Includes one active connection."}
            ],
        },
        "commercials_pricing": {
            "plans": [
                {"name": "Advanced", "price_monthly": "$59", "billing_unit": "12 months",
                 "includes": ["all channels"]}
            ],
            "trial_policy": "Setup takes about 10 minutes.",
            "discount_matrix": [],
        },
        "value_prop_roi": {"primary_tagline": "Watch everything", "persona_messaging": []},
        "competitive_intel": {"battlecards": []},
        "implementation_support": {},
        "security_compliance": {
            "certifications": ["SOC2", "HIPAA"],
            "uptime_guarantee": "99.9% uptime for enterprise customers",
        },
        "guardrails_disqualifiers": {},
    }
    base.update(overrides)
    return base


# --- grounding -----------------------------------------------------------------


def test_a_price_present_in_the_source_is_accepted() -> None:
    check = verify_fact("pricing_advanced", "Advanced: $59 for twelve months", SOURCE)
    assert check.grounded, check.reason


def test_a_price_absent_from_the_source_is_rejected() -> None:
    check = verify_fact("pricing_advanced", "Advanced: $79 for twelve months", SOURCE)
    assert not check.grounded
    assert "$79" in check.reason


def test_a_number_is_verified_with_its_unit_not_bare() -> None:
    """"15" matched "May 15, 2025" on a promo page and vouched for a discount."""

    grounded = verify_fact("discount_volume", "Volume discount: up to 15%", SOURCE)
    assert grounded.grounded, grounded.reason

    invented = verify_fact("discount_volume", "Volume discount: up to 40%", SOURCE)
    assert not invented.grounded
    assert "40 %" in invented.reason


def test_a_normalised_price_matches_the_page_that_omits_trailing_zeros() -> None:
    """Models write "$25.00" for a page that says "$ 25"; both are the same price.

    Demanding the trailing zeros rejected every genuine price on iptv.shopping
    and blocked activation on facts that were perfectly well grounded.
    """

    source = "Our Starter Package is $ 25 for three months of full access."
    assert verify_fact("pricing_starter", "Starter: $25.00 per 3 months", source).grounded
    assert verify_fact("pricing_starter", "Starter: $25 per 3 months", source).grounded
    # A real difference must still be caught.
    assert not verify_fact("pricing_starter", "Starter: $25.50 per 3 months", source).grounded
    assert not verify_fact("pricing_starter", "Starter: $99.00 per 3 months", source).grounded


def test_an_invented_certification_is_rejected() -> None:
    check = verify_fact("security_certifications", "Certified: SOC2, HIPAA, FEDRAMP", SOURCE)
    assert not check.grounded
    assert "FEDRAMP" in check.reason


def test_a_claim_cannot_corroborate_itself() -> None:
    """Subject terms come from the topic and proper nouns, never loose prose.

    Drawing them from the whole sentence let a fabricated claim supply the very
    words that would be searched for beside its number.

    Note the honest limit of proximity checking: it confirms a number appears in
    the right *region*, not that it is attached to the right subject. "$25" under
    a pricing heading supports any pricing claim. Catching a plan-to-price
    mix-up needs the provenance quote, not string matching.
    """

    terms = _subject_terms(
        "pricing_enterprise",
        "Enterprise tier costs $25 per month with unlimited concurrent premium streams",
    )
    assert "enterprise" in terms
    for loose_word in ("unlimited", "concurrent", "premium", "streams", "costs"):
        assert loose_word not in terms


def test_prose_without_numbers_or_acronyms_is_not_pretend_checked() -> None:
    check = verify_fact("value_tagline", "Watch everything you love", SOURCE)
    assert check.grounded
    assert "no numeric claim" in check.reason


# --- contract construction -----------------------------------------------------


def test_the_generated_contract_is_accepted_by_the_runtime() -> None:
    facts = {"pricing_advanced": "Advanced: $59 for twelve months"}
    contract = build_contract(
        kb(), facts, task_id="streamly_sales", agent_name="Adam",
        allowed_tools=["callback_schedule"], spoken_max_words=30, spoken_sentence_limit=2,
    )
    validated = TaskEngine.validate_contract(contract)
    assert validated["id"] == "streamly_sales"
    assert validated["knowledge"] == facts


def test_voice_settings_are_forced_not_taken_from_the_generator() -> None:
    contract = build_contract(
        kb(), {"a": "b"}, task_id="streamly_sales", agent_name="Adam",
        allowed_tools=[], spoken_max_words=30, spoken_sentence_limit=2,
    )
    assert contract["spoken_max_words"] == 30
    assert contract["spoken_sentence_limit"] == 2


def test_the_agent_name_appears_in_both_languages() -> None:
    contract = build_contract(
        kb(), {"a": "b"}, task_id="streamly_sales", agent_name="Adam",
        allowed_tools=[], spoken_max_words=30, spoken_sentence_limit=2,
    )
    assert "Adam" in contract["opening_greeting"]["en"]
    assert "Adam" in contract["opening_greeting"]["fr"]


def test_generated_slots_carry_no_invented_detect_patterns() -> None:
    """A wrong regex marks a question answered that the caller never answered."""

    contract = build_contract(
        kb(value_prop_roi={"primary_tagline": "x",
                           "persona_messaging": [{"role_title": "Operations Lead"}]}),
        {"a": "b"}, task_id="streamly_sales", agent_name="Adam",
        allowed_tools=[], spoken_max_words=30, spoken_sentence_limit=2,
    )
    generated = [s for s in contract["inputs_required"] if s["id"] != "permission_to_continue"]
    assert generated
    assert all("detect" not in slot for slot in generated)


def test_facts_stay_within_the_contract_limits() -> None:
    huge = kb()
    huge["core_specs"]["features"] = [
        {"name": f"Feature {i}", "description": "x" * 900} for i in range(30)
    ]
    facts = candidate_facts(huge)
    assert len(facts) <= TaskEngine.MAX_ENTRIES_PER_FIELD
    assert all(len(v) <= TaskEngine.MAX_ENTRY_CHARS for v in facts.values())


# --- the auto-apply gate -------------------------------------------------------


def write(tmp_path, knowledge_base: dict, source: str = SOURCE):
    kb_path = tmp_path / "kb.json"
    src_path = tmp_path / "source.md"
    kb_path.write_text(json.dumps(knowledge_base), encoding="utf-8")
    src_path.write_text(source, encoding="utf-8")
    return kb_path, src_path


def test_a_fully_grounded_product_can_activate_itself(tmp_path) -> None:
    kb_path, src_path = write(tmp_path, kb())
    report = import_product(
        kb_path, src_path, task_id="streamly_sales",
        implemented_tools={"callback_schedule"},
    )
    assert report.can_auto_apply, report.blocking
    assert report.contract["knowledge"]


def test_an_unverifiable_price_is_dropped_and_still_activates(tmp_path) -> None:
    """The invented price never reaches the caller either way.

    It is dropped from the knowledge block, so the agent cannot say it; asked
    about it, the lookup returns nothing and the agent admits the gap. Blocking
    the whole contract as well only cost an activation.
    """

    bad = kb()
    bad["commercials_pricing"]["plans"][0]["price_monthly"] = "$999"
    kb_path, src_path = write(tmp_path, bad)

    report = import_product(
        kb_path, src_path, task_id="streamly_sales",
        implemented_tools={"callback_schedule"},
    )

    assert report.can_auto_apply, report.blocking
    assert any("pricing" in warning for warning in report.warnings)
    assert not any("999" in fact for fact in report.contract["knowledge"].values())


def test_strict_mode_still_refuses_an_unverifiable_price(tmp_path) -> None:
    """Opt back in when a human should see a bad price before it goes live."""

    bad = kb()
    bad["commercials_pricing"]["plans"][0]["price_monthly"] = "$999"
    kb_path, src_path = write(tmp_path, bad)

    report = import_product(
        kb_path, src_path, task_id="streamly_sales",
        implemented_tools={"callback_schedule"}, strict=True,
    )

    assert not report.can_auto_apply
    assert any("pricing" in reason for reason in report.blocking)


def test_a_thin_crawl_blocks_activation(tmp_path) -> None:
    """Too few surviving facts means there is nothing to sell from."""

    kb_path, src_path = write(tmp_path, kb(), source="Nothing useful here.")
    report = import_product(
        kb_path, src_path, task_id="streamly_sales",
        implemented_tools={"callback_schedule"},
    )
    assert not report.can_auto_apply
    assert any("survived verification" in reason for reason in report.blocking)


def test_a_dropped_non_critical_fact_only_warns(tmp_path) -> None:
    """A missing feature degrades into honesty; it does not block the call."""

    partial = kb()
    partial["core_specs"]["features"] = [
        {"name": "Turbo", "description": "Streams at 4000 frames per second."}
    ]
    kb_path, src_path = write(tmp_path, partial)
    report = import_product(
        kb_path, src_path, task_id="streamly_sales",
        implemented_tools={"callback_schedule"},
    )
    assert report.can_auto_apply, report.blocking
    assert any("feature_turbo" in warning for warning in report.warnings)
    assert "feature_turbo" not in report.contract["knowledge"]


def test_only_implemented_tools_reach_the_contract(tmp_path) -> None:
    """A contract promising a tool nobody wrote makes the agent bluff."""

    kb_path, src_path = write(tmp_path, kb())
    report = import_product(
        kb_path, src_path, task_id="streamly_sales",
        implemented_tools={"callback_schedule", "send_checkout_link"},
    )
    assert report.contract["allowed_tools"] == ["callback_schedule", "send_checkout_link"]


def test_no_tools_is_a_warning_not_a_block(tmp_path) -> None:
    kb_path, src_path = write(tmp_path, kb())
    report = import_product(kb_path, src_path, task_id="streamly_sales", implemented_tools=set())
    assert report.can_auto_apply, report.blocking
    assert any("no tools" in warning for warning in report.warnings)


@pytest.mark.parametrize("task_id", ["Bad Id", "x", "has-dashes"])
def test_an_invalid_task_id_blocks_rather_than_writing_junk(tmp_path, task_id: str) -> None:
    kb_path, src_path = write(tmp_path, kb())
    report = import_product(kb_path, src_path, task_id=task_id, implemented_tools=set())
    assert not report.can_auto_apply
    assert any("rejected the generated contract" in reason for reason in report.blocking)


# --- sales intelligence ---------------------------------------------------------


def test_spoken_objection_answers_face_the_same_grounding_gate(tmp_path) -> None:
    """An objection answer is said out loud, so its numbers must be real.

    This is what stops market knowledge leaking into spoken claims: a competitor's
    price learned from the category is not a fact this company can stand behind.
    """

    import json as _json

    kb_path, src_path = write(tmp_path, kb())
    intel = tmp_path / "si.json"
    intel.write_text(_json.dumps({
        "objections": [
            {"objection": "Too costly", "answer": "It is only $59 for 12 months.",
             "source": "faq"},
            {"objection": "Rivals are cheaper", "answer": "They charge $3 a minute.",
             "source": "market"},
        ],
    }), encoding="utf-8")

    report = import_product(
        kb_path, src_path, task_id="streamly_sales",
        sales_intelligence_path=intel, implemented_tools={"callback_schedule"},
    )

    kept = [o["objection"] for o in report.contract.get("objection_playbook", [])]
    assert kept == ["Too costly"]
    assert any("objection answer" in warning for warning in report.warnings)


def test_learned_phrases_and_questions_reach_the_contract(tmp_path) -> None:
    import json as _json

    kb_path, src_path = write(tmp_path, kb())
    intel = tmp_path / "si.json"
    intel.write_text(_json.dumps({
        "sample_phrases": {"stating_price": "Fifty-nine for the year, all in."},
        "discovery_questions": ["What are you watching most?", "How many screens?"],
    }), encoding="utf-8")

    report = import_product(
        kb_path, src_path, task_id="streamly_sales",
        sales_intelligence_path=intel, implemented_tools=set(),
    )

    assert report.contract["sample_phrases"]["stating_price"]["en"].startswith("Fifty-nine")
    assert [s["id"] for s in report.contract["inputs_required"]][1:] == [
        "discovery_1", "discovery_2"
    ]


def test_a_missing_or_broken_intelligence_file_is_survivable(tmp_path) -> None:
    """Without it the agent is accurate but generic, which still works."""

    kb_path, src_path = write(tmp_path, kb())
    broken = tmp_path / "si.json"
    broken.write_text("{not json", encoding="utf-8")

    for path in (broken, tmp_path / "absent.json", None):
        report = import_product(
            kb_path, src_path, task_id="streamly_sales",
            sales_intelligence_path=path, implemented_tools=set(),
        )
        assert report.can_auto_apply, report.blocking
        assert "objection_playbook" not in report.contract


def test_the_website_address_is_a_speakable_fact(tmp_path) -> None:
    """Asked where to buy, the agent said it had no website address to give.

    Every spoken fact must come from the knowledge block, and nothing in the
    seven pillars carried the company's own address.
    """

    knowledge_base = kb()
    knowledge_base["website_url"] = "https://streamly.example"
    knowledge_base["company_name"] = "Streamly Media"
    facts = candidate_facts(knowledge_base)

    assert "streamly.example" in facts["website_address"]
    assert "Streamly Media" in facts["website_address"]
    # Spoken aloud, so no scheme and no trailing slash.
    assert "https://" not in facts["website_address"]


def test_a_missing_website_is_simply_absent(tmp_path) -> None:
    knowledge_base = kb()
    knowledge_base.pop("website_url", None)
    assert "website_address" not in candidate_facts(knowledge_base)
