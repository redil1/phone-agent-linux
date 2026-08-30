"""
Automated Test Suite for the 7-Pillar Product Knowledge & Sales Agent Pipeline.
"""

import asyncio
import os
import shutil
import pytest
import yaml

from src.schemas.product_schema import (
    ProductKnowledgeBase,
    CoreSpecsCapabilities,
    CommercialsPricing,
    ValuePropROI,
    CompetitiveIntelligence,
    ImplementationSupport,
    SecurityCompliance,
    GuardrailsDisqualifiers,
    FeatureItem,
    PlanTier,
    DiscountRule,
    PersonaMessaging,
    PainPointSolution,
    CompetitorBattlecard
)
from src.schemas.sales_skills_schema import SalesPlaybook
from src.sales_skills.sales_psychology import get_core_sales_playbook
from src.sales_skills.gtm_playbooks import enrich_playbook_with_product_context
from src.crawler.html_parser import HTMLToMarkdownParser
from src.extractor.extractor import ProductExtractor
from src.extractor.llm_client import LLMClient
from src.compiler.compiler import AgentCompiler


@pytest.fixture
def sample_kb():
    return ProductKnowledgeBase(
        product_name="VocalisAI",
        company_name="Vocalis Technologies Inc.",
        website_url="https://vocalis.ai",
        tagline="Autonomous AI Phone Sales Engine for High-Velocity Teams",
        core_specs=CoreSpecsCapabilities(
            summary="VocalisAI is an ultra-low latency (<300ms) autonomous voice sales agent that dials, qualifies, and books meetings.",
            features=[
                FeatureItem(
                    name="Sub-300ms Voice Pipeline",
                    category="Voice Core",
                    description="Streaming STT and neural TTS with natural conversational inflections.",
                    capabilities=["Barge-in interruption handling", "Background noise suppression"],
                    benefits=["Prospects cannot distinguish it from a top-tier human rep"],
                    tier_availability=["Starter", "Pro", "Enterprise"]
                ),
                FeatureItem(
                    name="Live CRM Bidirectional Sync",
                    category="Integrations",
                    description="Real-time read and write to Salesforce and HubSpot during active calls.",
                    capabilities=["Instant contact lookup", "Auto-logging call transcripts and MEDDPICC score"],
                    benefits=["Zero manual CRM admin for sales managers"],
                    tier_availability=["Pro", "Enterprise"]
                )
            ]
        ),
        commercials_pricing=CommercialsPricing(
            plans=[
                PlanTier(
                    name="Starter",
                    price_monthly="$99/mo",
                    price_annual="$79/mo",
                    billing_unit="per user",
                    seat_minimum=1,
                    included_features=["1,000 call minutes", "Standard voices", "Email support"],
                    excluded_features=["Custom voice clone", "Live CRM sync"],
                    best_for="Individual SDRs and founders"
                ),
                PlanTier(
                    name="Pro",
                    price_monthly="$299/mo",
                    price_annual="$249/mo",
                    billing_unit="per user",
                    seat_minimum=3,
                    included_features=["5,000 call minutes", "CRM sync", "Custom voice cloning", "Priority support"],
                    excluded_features=["Dedicated infrastructure"],
                    best_for="Scaling sales teams"
                )
            ],
            discount_matrix=[
                DiscountRule(scenario="Annual Upfront Commitment", max_discount_pct=20, conditions="Billed annually"),
                DiscountRule(scenario="Competitor Switcher", max_discount_pct=15, conditions="Showing competitor invoice")
            ]
        ),
        value_prop_roi=ValuePropROI(
            primary_tagline="10x your sales call volume and triple qualified booked meetings.",
            persona_messaging=[
                PersonaMessaging(
                    role_title="VP of Sales",
                    primary_pain_points=["Rep burnout", "Slow speed-to-lead"],
                    tailored_pitch="VocalisAI dials inbound leads in under 10 seconds, booking meetings before your competitors wake up.",
                    key_metrics_they_care_about=["Speed-to-lead", "Meeting booked rate"]
                )
            ],
            pain_point_matrix=[
                PainPointSolution(
                    problem="Human reps take 4+ hours to follow up with inbound leads.",
                    root_cause="Manual queuing and multitasking.",
                    product_solution="Autonomous sub-10 second instant outbound dialing.",
                    quantifiable_outcome="Increases lead qualification rate by 380%."
                )
            ],
            roi_benchmarks={"average_payback_period": "28 days", "pipeline_lift": "3.5x"}
        ),
        competitive_intel=CompetitiveIntelligence(
            battlecards=[
                CompetitorBattlecard(
                    competitor_name="LegacyBot",
                    competitor_tier="Legacy IVR",
                    their_weaknesses=["1.5-second latency", "Robotic monotone voice"],
                    our_distinct_advantages=["Sub-300ms latency", "Emotionally adaptive neural speech"],
                    pricing_comparison="2x cheaper with no hidden telephony surcharges.",
                    killer_question_to_ask="Have you noticed prospects hanging up because of the 1-second awkward pause?",
                    why_customers_switch="Embarrassment over robotic voice quality."
                )
            ]
        ),
        implementation_support=ImplementationSupport(
            time_to_value_timeline="Live in 15 minutes.",
            onboarding_milestones=["Connect SIP Trunk", "Upload Lead CSV", "Launch Autopilot Campaign"]
        ),
        security_compliance=SecurityCompliance(
            certifications=["SOC 2 Type II", "HIPAA Ready", "GDPR Compliant"],
            data_hosting_provider="AWS US-East-1 (Encrypted KMS)",
            encryption_standards="AES-256 at rest, TLS 1.3 in transit"
        ),
        guardrails_disqualifiers=GuardrailsDisqualifiers(
            unsupported_features=["Physical landline copper wire installation"],
            out_of_scope_use_cases=["Spam cold-calling without DNC scrubbing"],
            disqualification_criteria=["No CRM or digital lead source", "Budget < $50/mo"]
        )
    )


def test_html_to_markdown_parser():
    parser = HTMLToMarkdownParser()
    raw_html = """
    <html>
        <head><title>Test Product</title><style>body { color: red; }</style></head>
        <body>
            <nav><a href="/home">Home</a></nav>
            <h1>Revolutionary Sales AI</h1>
            <p>Our platform delivers <strong>sub-second voice calls</strong> for modern SDRs.</p>
            <ul>
                <li>Ultra low latency</li>
                <li>Zero hallucination</li>
            </ul>
            <footer>Copyright 2026</footer>
        </body>
    </html>
    """
    md = parser.html_to_markdown(raw_html)
    assert "Revolutionary Sales AI" in md
    assert "sub-second voice calls" in md
    assert "Ultra low latency" in md
    assert "Copyright 2026" not in md
    assert "style" not in md


def test_product_schema_validation(sample_kb):
    assert sample_kb.product_name == "VocalisAI"
    assert len(sample_kb.core_specs.features) == 2
    assert len(sample_kb.commercials_pricing.plans) == 2
    assert sample_kb.security_compliance.certifications[0] == "SOC 2 Type II"


def test_sales_playbook_generation(sample_kb):
    playbook = get_core_sales_playbook(sample_kb.product_name, sample_kb.value_prop_roi.primary_tagline)
    enriched = enrich_playbook_with_product_context(
        playbook=playbook,
        product_name=sample_kb.product_name,
        target_personas=sample_kb.value_prop_roi.persona_messaging,
        discounts=sample_kb.commercials_pricing.discount_matrix
    )
    assert len(enriched.discovery_questions) >= 5
    assert len(enriched.objection_library) >= 5
    assert len(enriched.closing_frameworks) >= 3
    assert "VP of Sales" in enriched.icp_summary


def test_3tier_compiler(sample_kb, tmp_path):
    output_dir = str(tmp_path / "test_dist")
    playbook = get_core_sales_playbook(sample_kb.product_name, sample_kb.value_prop_roi.primary_tagline)

    compiler = AgentCompiler(output_dir=output_dir)
    compiled = compiler.compile_all(sample_kb, playbook)

    # 1. Check Tier 1 Hot YAML
    assert os.path.exists(compiled["tier1_hot_yaml"])
    with open(compiled["tier1_hot_yaml"], "r") as f:
        hot_yaml_content = f.read()
        loaded = yaml.safe_load(hot_yaml_content)
        assert loaded["product"]["name"] == "VocalisAI"
        assert "starter" in loaded["commercials"]["plans"]
        assert "Annual Upfront Commitment" in loaded["commercials"]["discount_rules"]

    # 2. Check Tier 2 Fast JSON
    assert os.path.exists(compiled["tier2_fast_json"])
    with open(compiled["tier2_fast_json"], "r") as f:
        import json
        fast_json = json.load(f)
        assert "sub-300ms_voice_pipeline" in fast_json["features"]
        assert "legacybot" in fast_json["competitors"]

    # 3. Check Tier 3 Edge Case MD
    assert os.path.exists(compiled["tier3_edge_md"])
    with open(compiled["tier3_edge_md"], "r") as f:
        edge_md = f.read()
        assert "SOC 2 Type II" in edge_md
        assert "category: security_compliance" in edge_md

    # 4. Check Master Voice Agent Prompt
    assert os.path.exists(compiled["voice_agent_prompt"])
    with open(compiled["voice_agent_prompt"], "r") as f:
        prompt_text = f.read()
        assert "AUTONOMOUS PHONE SALES AGENT" in prompt_text
        assert "VocalisAI" in prompt_text
        assert "THE 4-STEP LOOP" in prompt_text


@pytest.mark.asyncio
async def test_extractor_real_validation(sample_kb):
    # Mock LLM Client that returns valid 7-pillar JSON
    class MockLLMClient:
        def __init__(self):
            self.provider = "mock_ollama"
            self.model = "kimi-2.6"

        async def generate_json(self, system_prompt, user_prompt):
            return sample_kb.model_dump()

    extractor = ProductExtractor(llm_client=MockLLMClient())
    sample_text = "This is a comprehensive crawled website page about VocalisAI with all product details..."
    
    kb = await extractor.extract_from_markdown(
        crawled_markdown=sample_text,
        website_url="https://vocalis.ai",
        product_name_hint="VocalisAI"
    )
    assert kb.product_name == "VocalisAI"
    assert len(kb.core_specs.features) == 2
    assert len(kb.commercials_pricing.plans) == 2


@pytest.mark.asyncio
async def test_extractor_raises_on_failure():
    # Mock LLM Client that simulates connection failure
    class BrokenLLMClient:
        def __init__(self):
            self.provider = "ollama"
            self.model = "kimi"

        async def generate_json(self, system_prompt, user_prompt):
            raise RuntimeError("Failed to communicate with Ollama at http://localhost:11434: Connection Refused")

    extractor = ProductExtractor(llm_client=BrokenLLMClient())
    
    with pytest.raises(RuntimeError) as exc_info:
        await extractor.extract_from_markdown(
            crawled_markdown="Sample crawled website text with lots of content...",
            website_url="https://example.com"
        )
    assert "Connection Refused" in str(exc_info.value)



def test_fastapi_endpoints():
    # The engine's own web UI is optional and unused when the research engine is
    # driven from the PhoneAgent Studio, so fastapi may not be installed.
    pytest = __import__("pytest")
    pytest.importorskip("fastapi", reason="engine web UI is optional")

    from fastapi.testclient import TestClient
    from src.server import app

    client = TestClient(app)
    
    # 1. Test index page
    res = client.get("/")
    assert res.status_code == 200
    assert "Autonomous AI Sales Product Intelligence" in res.text

    # 2. Test Ollama status check endpoint
    res_ollama = client.get("/api/ollama/status")
    assert res_ollama.status_code == 200
    data = res_ollama.json()
    assert "online" in data
    assert "models" in data

    # 3. Test Voice Simulation Endpoint
    sim_payload = {
        "user_message": "Your platform is too expensive for our budget right now.",
        "kb_data": {
            "product_name": "VocalisAI",
            "tagline": "Real-time voice sales engine",
            "value_prop_roi": {"primary_tagline": "Triple booked meetings"}
        }
    }
    res_sim = client.post("/api/simulate-voice-turn", json=sim_payload)
    assert res_sim.status_code == 200
    sim_data = res_sim.json()
    assert "Price & Budget" in sim_data["intent"]
    assert "VocalisAI" in sim_data["agent_response"]
    assert "4-Step Objection Loop" in sim_data["step_executed"]

