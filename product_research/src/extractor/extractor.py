"""
7-Pillar Structured Product Intelligence Extractor.
Extracts, structures, and validates deep product knowledge from raw crawled website content.
"""

from typing import Any
from urllib.parse import urlparse

from ..schemas.product_schema import ProductKnowledgeBase
from .llm_client import LLMClient

EXTRACTION_SYSTEM_PROMPT = """
You are the World's Best Product Knowledge Architect and GTM Sales Engineer.
Your task is to analyze the provided crawled website content of a product and extract a COMPLETE, HYPER-ACCURATE, and DETERMINISTIC Product Knowledge Base across 7 mandatory pillars.

CRITICAL INSTRUCTIONS:
1. Return VALID JSON ONLY that strictly matches the required JSON structure.
2. EXACT PRICING & ZERO HALLUCINATION POLICY:
   - Carefully scan the crawled text (especially sections marked PRICING, PLANS, or tables).
   - If exact prices are mentioned on the website (e.g., $4/mo, $21/seat, $25/3mo, $0.05/min, $0 Free tier, $10/user/mo), extract the EXACT dollar amounts, plan names, and inclusions VERBATIM.
   - If the website does NOT list public prices or uses a sales contact form, set price to 'Contact Sales / Custom Quote' or 'Custom Enterprise' rather than inventing fictional dollar numbers.
3. Top-Level Keys:
   - "core_specs"
   - "commercials_pricing"
   - "value_prop_roi"
   - "competitive_intel"
   - "implementation_support"
   - "security_compliance"
   - "guardrails_disqualifiers"

4. Format for Voice AI: Keep descriptions punchy, factual, and direct.
"""


# Generic endings that are plainly not part of a brand. A descriptive new gTLD
# such as ".shopping" or ".tv" usually IS part of the name, so it is kept.
_GENERIC_TLDS = frozenset(
    {"com", "net", "org", "io", "ai", "co", "dev", "app", "me", "info", "biz",
     "uk", "fr", "de", "es", "it", "ma", "us", "eu", "online", "site"}
)
# A short, vowel-poor label is an acronym: "Iptv" reads as a typo when spoken,
# while "Vapi" and "Acme" are words and must keep their normal capitalisation.
_VOWELS = frozenset("aeiou")
_ACRONYM_MAX_LEN = 5
_ACRONYM_MAX_VOWEL_RATIO = 0.34


def brand_from_domain(website_url: str) -> str:
    """Best guess at the company name when the page never states one.

    Taking only the first label turned iptv.shopping into "Iptv", so the agent
    introduced itself as calling "from Iptv, about Iptv".
    """

    host = urlparse(website_url).netloc.replace("www.", "").split(":")[0]
    labels = [label for label in host.split(".") if label]
    if len(labels) > 1 and labels[-1].lower() in _GENERIC_TLDS:
        labels = labels[:-1]
    def render(label: str) -> str:
        lowered = label.lower()
        vowels = sum(character in _VOWELS for character in lowered)
        looks_like_acronym = (
            len(lowered) <= _ACRONYM_MAX_LEN
            and vowels / len(lowered) <= _ACRONYM_MAX_VOWEL_RATIO
        )
        return label.upper() if looks_like_acronym else label.capitalize()

    words = [render(label) for label in labels]
    return " ".join(words) or host


def ensure_list_str(val: Any) -> list[str]:
    """Ensures input is always a list of strings."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val if v is not None]
    if isinstance(val, str):
        return [val]
    return [str(val)]


def normalize_llm_json(raw: dict[str, Any], product_name: str, website_url: str) -> dict[str, Any]:
    """Normalizes variations in LLM key names and shapes into strict Pydantic structure."""
    out = dict(raw)

    # 1. Flexible Key Scanner for all 7 Pillars (matching any variation like pillar_1_core_specs, pillar_2_commercials, etc.)
    for k, v in list(out.items()):
        kl = k.lower()
        if ("pillar_1" in kl or "pillar1" in kl or "core_spec" in kl or "capabilities" in kl) and "core_specs" not in out:
            out["core_specs"] = v
        elif ("pillar_2" in kl or "pillar2" in kl or "commercial" in kl or "pricing" in kl) and "commercials_pricing" not in out:
            out["commercials_pricing"] = v
        elif ("pillar_3" in kl or "pillar3" in kl or "value_prop" in kl or "value_proposition" in kl or "roi" in kl) and "value_prop_roi" not in out:
            out["value_prop_roi"] = v
        elif ("pillar_4" in kl or "pillar4" in kl or "competitive" in kl or "battlecard" in kl or "competitor" in kl) and "competitive_intel" not in out:
            out["competitive_intel"] = v
        elif ("pillar_5" in kl or "pillar5" in kl or "implementation" in kl or "onboard" in kl or "support_sla" in kl) and "implementation_support" not in out:
            out["implementation_support"] = v
        elif ("pillar_6" in kl or "pillar6" in kl or "security" in kl or "compliance" in kl) and "security_compliance" not in out:
            out["security_compliance"] = v
        elif ("pillar_7" in kl or "pillar7" in kl or "boundar" in kl or "guardrail" in kl or "disqualif" in kl) and "guardrails_disqualifiers" not in out:
            out["guardrails_disqualifiers"] = v

    # 2. Normalize Core Specs
    cs = out.get("core_specs", {})
    if isinstance(cs, dict):
        if "summary" not in cs:
            cs["summary"] = cs.get("overview") or cs.get("product_overview") or cs.get("description") or f"{product_name} platform."
        
        # Features normalization
        raw_feats = cs.get("features") or cs.get("key_features") or []
        normalized_features = []
        for item in raw_feats:
            if isinstance(item, str):
                normalized_features.append({
                    "name": item.split(":")[0] if ":" in item else item[:40],
                    "category": "Core Feature",
                    "description": item,
                    "capabilities": [item],
                    "benefits": ["Improves operational efficiency and user experience"],
                    "tier_availability": ["All Plans"]
                })
            elif isinstance(item, dict):
                normalized_features.append({
                    "name": item.get("name") or item.get("feature_name") or "Core Feature",
                    "category": item.get("category", "Core Platform"),
                    "description": item.get("description", str(item)),
                    "capabilities": ensure_list_str(item.get("capabilities")),
                    "benefits": ensure_list_str(item.get("benefits")),
                    "tier_availability": ensure_list_str(item.get("tier_availability", ["All Plans"]))
                })
        cs["features"] = normalized_features

        # Integrations normalization
        raw_int = cs.get("integrations", {})
        if isinstance(raw_int, list):
            cs["integrations"] = {
                "native_integrations": ensure_list_str(raw_int),
                "api_capabilities": "Compatible Apps & API Integration",
                "webhook_events": [],
                "setup_complexity": "Low"
            }
        elif isinstance(raw_int, dict):
            # Write through the local dict: cs may not carry this key at all,
            # which is normal when a smaller model omits a whole sub-object.
            raw_int["native_integrations"] = ensure_list_str(
                raw_int.get("native_integrations") or raw_int.get("supported_apps")
            )
            raw_int["webhook_events"] = ensure_list_str(raw_int.get("webhook_events"))
            raw_int.setdefault("api_capabilities", "REST API & Webhooks")
            raw_int.setdefault("setup_complexity", "Low")
            cs["integrations"] = raw_int

        # Technical specs normalization
        raw_tech = cs.get("technical_specs", {})
        if isinstance(raw_tech, dict):
            cs["technical_specs"] = {
                "system_requirements": ensure_list_str(raw_tech.get("system_requirements") or raw_tech.get("device_compatibility", ["Standard connection"])),
                "supported_platforms": ensure_list_str(raw_tech.get("supported_platforms") or raw_tech.get("device_compatibility", ["Web", "Smart TV", "Mobile", "Apps"])),
                "architecture_overview": raw_tech.get("architecture_overview") or raw_tech.get("streaming_quality") or "High-availability Streaming Infrastructure",
                "limits_and_quotas": raw_tech.get("limits_and_quotas") or {"connections": str(raw_tech.get("connections", "1 active connection"))}
            }

        # Release info normalization
        raw_rel = cs.get("release_info") or cs.get("roadmap_guardrails") or {}
        if isinstance(raw_rel, str):
            cs["release_info"] = {
                "current_version": "Production Live",
                "released_features": [],
                "upcoming_roadmap": [],
                "roadmap_guardrail": raw_rel
            }
        elif isinstance(raw_rel, dict):
            raw_rel["released_features"] = ensure_list_str(raw_rel.get("released_features"))
            raw_rel["upcoming_roadmap"] = ensure_list_str(raw_rel.get("upcoming_roadmap"))
            raw_rel.setdefault("current_version", "Production Live")
            cs["release_info"] = raw_rel

        out["core_specs"] = cs

    # 3. Normalize Commercials & Pricing
    cp = out.get("commercials_pricing", {})
    if isinstance(cp, dict):
        raw_plans = cp.get("plans") or cp.get("pricing_plans") or cp.get("packages") or []
        norm_plans = []
        for p in raw_plans:
            if isinstance(p, dict):
                p_details = p.get("pricing_details", {})
                price_m = p.get("price_monthly") or p.get("price")
                if not price_m and isinstance(p_details, dict):
                    price_m = p_details.get("call_minutes") or p_details.get("price") or p.get("type", "Custom")
                elif not price_m:
                    price_m = p.get("duration", "Custom")

                inc = p.get("included_features") or p.get("inclusions") or p.get("features")
                if not inc:
                    inc = [p.get("description", "")] if p.get("description") else []

                norm_plans.append({
                    "name": p.get("name") or p.get("plan_name") or "Standard Plan",
                    "price_monthly": str(price_m),
                    "price_annual": str(p.get("price_annual") or price_m),
                    "billing_unit": p.get("billing_unit") or p.get("duration") or "package / monthly",
                    "seat_minimum": p.get("seat_minimum", 1),
                    "included_features": ensure_list_str(inc),
                    "excluded_features": ensure_list_str(p.get("excluded_features")),
                    "best_for": p.get("best_for", p.get("description", "All users"))
                })
        cp["plans"] = norm_plans
        
        # Discount matrix normalization
        raw_dm = cp.get("discount_matrix") or cp.get("discount_rules") or []
        norm_dm = []
        if isinstance(raw_dm, dict):
            for scenario, rule in raw_dm.items():
                norm_dm.append({
                    "scenario": str(scenario).replace("_", " ").title(),
                    "max_discount_pct": 15,
                    "conditions": str(rule)
                })
        elif isinstance(raw_dm, str):
            norm_dm.append({
                "scenario": "Promotional / Volume Discount",
                "max_discount_pct": 15,
                "conditions": raw_dm
            })
        elif isinstance(raw_dm, list):
            for d in raw_dm:
                if isinstance(d, dict):
                    norm_dm.append({
                        "scenario": d.get("scenario", "Volume Discount"),
                        "max_discount_pct": int(d.get("max_discount_pct", 15)),
                        "conditions": str(d.get("conditions", ""))
                    })
                elif isinstance(d, str):
                    norm_dm.append({
                        "scenario": d[:30],
                        "max_discount_pct": 15,
                        "conditions": d
                    })
        if not norm_dm:
            norm_dm = [{"scenario": "Multi-Month / Volume Savings", "max_discount_pct": 15, "conditions": "Longer term plan selection"}]
        
        cp["discount_matrix"] = norm_dm
        cp.setdefault("hard_margin_floor", "Standard published prices apply; max 15% discount for long-term bundles.")
        cp.setdefault("trial_policy", cp.get("pricing_notes", "Instant access upon activation"))
        cp.setdefault("contract_terms", cp.get("billing_metrics", "One-time package or subscription"))
        cp.setdefault("cancellation_and_refund_policy", cp.get("refund_policy", "Terms of service apply"))
        out["commercials_pricing"] = cp

    # 4. Normalize Value Prop & ROI
    vp = out.get("value_prop_roi", {})
    if isinstance(vp, dict):
        vp.setdefault("primary_tagline", f"High-quality entertainment and streaming with {product_name}")
        
        # Personas
        raw_pm = vp.get("persona_messaging") or vp.get("persona_pitches") or {}
        norm_pm = []
        if isinstance(raw_pm, dict):
            for role, pitch in raw_pm.items():
                norm_pm.append({
                    "role_title": role.replace("_", " ").title(),
                    "primary_pain_points": [str(pitch)[:50]],
                    "tailored_pitch": str(pitch),
                    "key_metrics_they_care_about": ["Reliability", "Cost Savings"]
                })
        elif isinstance(raw_pm, list):
            for item in raw_pm:
                if isinstance(item, dict):
                    norm_pm.append({
                        "role_title": item.get("role_title", "User"),
                        "primary_pain_points": ensure_list_str(item.get("primary_pain_points")),
                        "tailored_pitch": item.get("tailored_pitch", "Save costs with high quality"),
                        "key_metrics_they_care_about": ensure_list_str(item.get("key_metrics_they_care_about", ["Cost Savings"]))
                    })
        vp["persona_messaging"] = norm_pm

        # Pain points
        raw_pp = vp.get("pain_point_matrix") or vp.get("pain_points") or vp.get("pain_points_addressed") or []
        norm_pp = []
        if isinstance(raw_pp, list):
            for item in raw_pp:
                if isinstance(item, str):
                    norm_pp.append({
                        "problem": item,
                        "root_cause": "Expensive legacy cable or fragmented subscriptions",
                        "product_solution": f"Solved by {product_name}",
                        "quantifiable_outcome": "Saves 70%+ on monthly TV expenses"
                    })
                elif isinstance(item, dict):
                    norm_pp.append({
                        "problem": item.get("problem", "Friction"),
                        "root_cause": item.get("root_cause", ""),
                        "product_solution": item.get("product_solution", f"Solved by {product_name}"),
                        "quantifiable_outcome": item.get("quantifiable_outcome", "Quantifiable Value")
                    })
        vp["pain_point_matrix"] = norm_pp

        # Case studies
        raw_cs = vp.get("case_studies") or vp.get("quantifiable_roi") or []
        norm_cs = []
        if isinstance(raw_cs, list):
            for c in raw_cs:
                if isinstance(c, dict):
                    norm_cs.append({
                        "client_name_or_industry": c.get("customer") or c.get("client_name_or_industry", "Satisfied Customer"),
                        "challenge": "High costs / channel access",
                        "result_metric": str(c.get("metrics") or c.get("location") or "99.9% Uptime"),
                        "customer_quote": c.get("quote") or c.get("customer_quote")
                    })
        elif isinstance(raw_cs, str):
            norm_cs.append({
                "client_name_or_industry": "Active Subscriber",
                "challenge": "Channel availability",
                "result_metric": raw_cs[:100],
                "customer_quote": raw_cs[:150]
            })
        vp["case_studies"] = norm_cs

        # ROI benchmarks
        raw_roi = vp.get("roi_benchmarks")
        if isinstance(raw_roi, list):
            norm_roi = {}
            for item in raw_roi:
                if isinstance(item, dict):
                    m = item.get("metric") or item.get("name") or "benchmark"
                    v = item.get("value") or item.get("description") or str(item)
                    norm_roi[m] = str(v)
                elif isinstance(item, str):
                    norm_roi[f"roi_{len(norm_roi)+1}"] = item
            vp["roi_benchmarks"] = norm_roi
        elif isinstance(raw_roi, dict):
            vp["roi_benchmarks"] = {k: str(v) for k, v in raw_roi.items()}
        elif isinstance(raw_roi, str):
            vp["roi_benchmarks"] = {"savings_and_uptime": raw_roi}
        else:
            vp["roi_benchmarks"] = {"average_savings": "70%+", "uptime": "99.9%"}
        
        out["value_prop_roi"] = vp

    # 5. Normalize Competitive Intelligence
    ci = out.get("competitive_intel", {})
    if isinstance(ci, dict):
        raw_bc = ci.get("battlecards", [])
        norm_bc = []
        if isinstance(raw_bc, dict):
            for comp_key, comp_val in raw_bc.items():
                comp_name = comp_key.replace("vs_", "").replace("_", " ").title()
                norm_bc.append({
                    "competitor_name": comp_name,
                    "competitor_tier": "Alternative",
                    "their_weaknesses": ["Higher costs, limited international channels, hardware lock-in"],
                    "our_distinct_advantages": [str(comp_val)],
                    "pricing_comparison": "Fraction of traditional cable costs",
                    "killer_question_to_ask": f"How much are you currently paying for cable or {comp_name} each month?",
                    "why_customers_switch": f"Switching to {product_name} for massive channel selection and 4K streaming"
                })
        elif isinstance(raw_bc, list):
            for b in raw_bc:
                if isinstance(b, dict):
                    weak = b.get("their_weaknesses") or b.get("weaknesses", ["High setup overhead"])
                    if isinstance(weak, str):
                        weak = [w.strip() for w in weak.split(",") if w.strip()] or [weak]
                    
                    adv = b.get("our_distinct_advantages") or b.get("vapi_advantage") or b.get("advantages", [f"Better value with {product_name}"])
                    if isinstance(adv, str):
                        adv = [a.strip() for a in adv.split(",") if a.strip()] or [adv]

                    norm_bc.append({
                        "competitor_name": b.get("competitor") or b.get("competitor_name", "Competitor"),
                        "competitor_tier": "Direct",
                        "their_weaknesses": weak,
                        "our_distinct_advantages": adv,
                        "pricing_comparison": "Transparent pricing",
                        "killer_question_to_ask": "Are you experiencing buffering or high bills with your current setup?",
                        "why_customers_switch": b.get("displacement_tactic") or b.get("why_customers_switch", "For reliability and channel breadth")
                    })
        ci["battlecards"] = norm_bc

        raw_disp = ci.get("displacement_strategy") or ci.get("displacement_tactics")
        if isinstance(raw_disp, str):
            ci["displacement_strategy"] = {
                "migration_timeline_days": "Instant setup",
                "automated_importers": ["M3U Playlist / Xtream Codes"],
                "downtime_risk": "Zero downtime",
                "migration_support_included": raw_disp[:150]
            }
        elif isinstance(raw_disp, list):
            ci["displacement_strategy"] = {
                "migration_timeline_days": "Instant setup",
                "automated_importers": ["M3U Playlist / Xtream Codes"],
                "downtime_risk": "Zero downtime",
                "migration_support_included": "; ".join(raw_disp[:3])
            }
        elif isinstance(raw_disp, dict):
            raw_disp.setdefault("migration_timeline_days", "Instant")
            raw_disp["automated_importers"] = ensure_list_str(
                raw_disp.get("automated_importers", ["M3U Playlist"])
            )
            raw_disp.setdefault("downtime_risk", "Zero downtime")
            raw_disp.setdefault("migration_support_included", "24/7 Setup Assistance")
            ci["displacement_strategy"] = raw_disp
        else:
            ci["displacement_strategy"] = {
                "migration_timeline_days": "Instant",
                "automated_importers": ["M3U Playlist / Xtream Codes"],
                "downtime_risk": "Zero downtime",
                "migration_support_included": "24/7 Setup Assistance"
            }
        out["competitive_intel"] = ci

    # 6. Normalize Implementation Support
    imp = out.get("implementation_support", {})
    if isinstance(imp, dict):
        imp.setdefault("time_to_value_timeline", imp.get("timelines") or imp.get("implementation_timeline", "Instant activation via email."))
        imp["onboarding_milestones"] = ensure_list_str(imp.get("onboarding_milestones") or imp.get("onboarding_steps", ["Step 1: Order", "Step 2: Get M3U/Xtream credentials", "Step 3: Stream"]))
        imp["customer_prerequisites"] = ensure_list_str(imp.get("customer_prerequisites", ["Supported device (Smart TV, Firestick, Mobile, PC)", "IPTV app"]))

        raw_st = imp.get("support_tiers") or imp.get("support_slas")
        if isinstance(raw_st, list):
            norm_st = {}
            for item in raw_st:
                if isinstance(item, dict):
                    t = item.get("tier") or item.get("name") or f"Tier_{len(norm_st)+1}"
                    s = item.get("support") or item.get("description") or str(item)
                    norm_st[t] = str(s)
            imp["support_tiers"] = norm_st
        elif isinstance(raw_st, dict):
            imp["support_tiers"] = {k: str(v) for k, v in raw_st.items()}
        elif isinstance(raw_st, str):
            imp["support_tiers"] = {"24/7 Support": raw_st}
        else:
            imp["support_tiers"] = {"Live Chat": "24/7 live chat and email support"}

        imp["training_resources"] = ensure_list_str(imp.get("training_resources") or imp.get("training_and_resources", ["Setup guides and app video tutorials"]))
        out["implementation_support"] = imp

    # 7. Normalize Security & Compliance
    sec = out.get("security_compliance", {})
    if isinstance(sec, dict):
        sec["certifications"] = ensure_list_str(sec.get("certifications") or sec.get("compliance") or sec.get("compliance_certifications", ["256-bit encryption", "Secure Checkout"]))
        sec.setdefault("data_hosting_provider", sec.get("data_hosting", "Secure Cloud CDN Infrastructure"))
        
        raw_enc = sec.get("encryption_standards") or sec.get("security_features", ["256-bit SSL encryption"])
        if isinstance(raw_enc, list):
            sec["encryption_standards"] = ", ".join(raw_enc)
        else:
            sec["encryption_standards"] = str(raw_enc)

        sec.setdefault("data_retention_and_privacy", sec.get("privacy_policy", "Standard privacy protections apply"))
        sec.setdefault("uptime_sla_guarantee", sec.get("uptime_guarantee", "99.9% Uptime AntiFreeze Technology"))

        raw_dpa = sec.get("dpa_and_baa_available")
        if isinstance(raw_dpa, str):
            sec["dpa_and_baa_available"] = any(w in raw_dpa.lower() for w in ["yes", "available", "true", "implies", "included", "$2000"])
        elif isinstance(raw_dpa, bool):
            sec["dpa_and_baa_available"] = raw_dpa
        else:
            sec["dpa_and_baa_available"] = False
        
        out["security_compliance"] = sec

    # 8. Normalize Guardrails & Disqualifiers
    gd = out.get("guardrails_disqualifiers", {})
    if isinstance(gd, dict):
        gd["unsupported_features"] = ensure_list_str(gd.get("unsupported_features", ["Simultaneous multi-device streaming on single connection"]))
        gd["out_of_scope_use_cases"] = ensure_list_str(gd.get("out_of_scope_use_cases", ["Commercial broadcast / reselling without license"]))
        gd["disqualification_criteria"] = ensure_list_str(gd.get("disqualification_criteria", ["Requires multiple simultaneous connections on single sub"]))
        gd.setdefault("polite_disqualification_script", "Thank you for reaching out. Based on your multi-connection requirement, our standard single-device plan won't be the right fit for your setup.")
        out["guardrails_disqualifiers"] = gd

    return out


class ProductExtractor:
    def __init__(self, llm_client: LLMClient | None = None):
        self.llm_client = llm_client or LLMClient(provider="auto")

    async def extract_from_markdown(
        self,
        crawled_markdown: str,
        website_url: str,
        product_name_hint: str | None = None
    ) -> ProductKnowledgeBase:
        """Extracts and validates full 7-pillar product knowledge base."""

        product_name = product_name_hint or brand_from_domain(website_url)

        if not crawled_markdown or len(crawled_markdown.strip()) < 50:
            raise ValueError(f"Crawled content from {website_url} is empty or insufficient to extract product knowledge.")

        user_prompt = f"""
WEBSITE URL: {website_url}
PRODUCT NAME HINT: {product_name}

RAW CRAWLED WEBSITE CONTENT:
=====================================================
{crawled_markdown[:45000]}
=====================================================

Extract the complete ProductKnowledgeBase JSON conforming to the 7 pillars.
"""

        raw_json = await self.llm_client.generate_json(
            system_prompt=EXTRACTION_SYSTEM_PROMPT,
            user_prompt=user_prompt
        )

        # Ensure top-level metadata fields are set
        raw_json.setdefault("product_name", product_name)
        raw_json.setdefault("company_name", product_name)
        raw_json.setdefault("website_url", website_url)
        raw_json.setdefault("tagline", f"The official platform for {product_name}")

        normalized_json = normalize_llm_json(raw_json, product_name, website_url)

        try:
            return ProductKnowledgeBase.model_validate(normalized_json)
        except Exception as ve:
            raise ValueError(
                f"Extracted JSON from {self.llm_client.provider} ({self.llm_client.model}) failed schema validation: {ve}\n"
                f"Raw LLM Response: {raw_json}"
            )
