"""
Product Knowledge Base Schema - The 7 Pillars of Product Intelligence for Autonomous Sales AI.
"""

from pydantic import BaseModel, Field

# ---------------------------------------------------------
# Pillar 1: Core Specs & Capabilities
# ---------------------------------------------------------

class FeatureItem(BaseModel):
    name: str = Field(..., description="Feature or module name")
    category: str = Field("Core", description="Category or functional grouping (e.g. Analytics, Automation, Voice)")
    description: str = Field(..., description="Clear explanation of how the feature works")
    capabilities: list[str] = Field(default_factory=list, description="Specific things this feature allows the user to do")
    benefits: list[str] = Field(default_factory=list, description="Direct business/operational benefits")
    is_add_on: bool = Field(False, description="True if this is an optional paid add-on, False if included")
    tier_availability: list[str] = Field(default_factory=list, description="Plans where this feature is available (e.g. ['Pro', 'Enterprise'])")


class TechnicalSpecs(BaseModel):
    system_requirements: list[str] = Field(default_factory=list, description="Browser, OS, or hardware requirements")
    supported_platforms: list[str] = Field(default_factory=list, description="Web, iOS, Android, macOS, Windows, Linux")
    architecture_overview: str = Field("", description="High-level architecture (e.g. Cloud-native SaaS, Edge API)")
    limits_and_quotas: dict[str, str] = Field(default_factory=dict, description="Hard caps (e.g. {'api_rate_limit': '100 req/sec', 'concurrency': '50 calls'})")


class IntegrationEcosystem(BaseModel):
    native_integrations: list[str] = Field(default_factory=list, description="Out-of-the-box integrations (e.g. Salesforce, HubSpot, Stripe)")
    api_capabilities: str = Field("", description="REST API, GraphQL, Webhooks, SDKs availability")
    webhook_events: list[str] = Field(default_factory=list, description="Key webhook triggers supported")
    setup_complexity: str = Field("Low", description="Low (1-click / OAuth), Medium (API Key), High (Custom Dev)")


class ReleaseRoadmap(BaseModel):
    current_version: str = Field("Latest", description="Current live production version")
    released_features: list[str] = Field(default_factory=list, description="Recently shipped features")
    upcoming_roadmap: list[str] = Field(default_factory=list, description="Planned future features")
    roadmap_guardrail: str = Field(
        "CRITICAL: Never promise upcoming features as live. State: 'It is on our near-term roadmap for Q3/Q4, but today we focus on [live feature]'",
        description="Rule for how the agent speaks about non-live items"
    )


class CoreSpecsCapabilities(BaseModel):
    summary: str = Field(..., description="2-3 sentence overview of what the product does")
    features: list[FeatureItem] = Field(default_factory=list, description="Full feature catalog")
    technical_specs: TechnicalSpecs = Field(default_factory=TechnicalSpecs)
    integrations: IntegrationEcosystem = Field(default_factory=IntegrationEcosystem)
    release_info: ReleaseRoadmap = Field(default_factory=ReleaseRoadmap)


# ---------------------------------------------------------
# Pillar 2: Commercials, Pricing & Packaging
# ---------------------------------------------------------

class PlanTier(BaseModel):
    name: str = Field(..., description="Plan name (e.g. Starter, Growth, Enterprise)")
    price_monthly: str | None = Field(None, description="Monthly billing price (e.g. '$99/mo')")
    price_annual: str | None = Field(None, description="Annual billing price / monthly equivalent (e.g. '$79/mo billed annually')")
    billing_unit: str = Field("per user/month", description="Pricing metric (e.g. per seat, per 1,000 credits, flat fee)")
    seat_minimum: int = Field(1, description="Minimum seats or commitment required")
    included_features: list[str] = Field(default_factory=list, description="Key features included in this tier")
    excluded_features: list[str] = Field(default_factory=list, description="Features locked out or requiring upgrade")
    best_for: str = Field("", description="Ideal buyer profile for this tier")


class DiscountRule(BaseModel):
    scenario: str = Field(..., description="Trigger (e.g. 'Annual upfront payment', 'Multi-year commitment', 'Competitor switch')")
    max_discount_pct: int = Field(..., description="Maximum allowed discount percentage without manager approval")
    coupon_code: str | None = Field(None, description="Promo code to apply if applicable")
    conditions: str = Field("", description="Conditions (e.g. 'Must pay full year upfront')")


class CommercialsPricing(BaseModel):
    plans: list[PlanTier] = Field(default_factory=list, description="Available pricing tiers")
    pricing_model_type: str = Field("Per Seat / Subscription", description="Subscription, Usage-based, Flat-rate, Hybrid")
    overage_rules: str = Field("", description="What happens when quotas are exceeded")
    discount_matrix: list[DiscountRule] = Field(default_factory=list, description="Pre-approved discount thresholds")
    hard_margin_floor: str = Field("Never exceed 25% discount under any circumstance without VP Sales approval.", description="Absolute discount ceiling")
    payment_terms: str = Field("Credit Card, ACH, Wire Transfer (for Enterprise >$5k)", description="Accepted payment methods")
    trial_policy: str = Field("14-day free trial, no credit card required", description="Trial length and terms")
    contract_terms: str = Field("Monthly rolling or 12-month annual commitment", description="Contract duration")
    cancellation_and_refund_policy: str = Field(
        "Cancel anytime before next billing cycle. 30-day money-back guarantee for initial annual purchases.",
        description="Cancellation & refund rules"
    )


# ---------------------------------------------------------
# Pillar 3: Value Proposition & ROI Data
# ---------------------------------------------------------

class PersonaMessaging(BaseModel):
    role_title: str = Field(..., description="Target role (e.g. CEO / Founder, VP of Sales, CTO, Head of Operations)")
    primary_pain_points: list[str] = Field(default_factory=list, description="What keeps them up at night")
    tailored_pitch: str = Field(..., description="1-2 sentence pitch tailored specifically to their priorities")
    key_metrics_they_care_about: list[str] = Field(default_factory=list, description="e.g. CAC reduction, pipeline velocity, uptime, compliance")


class PainPointSolution(BaseModel):
    problem: str = Field(..., description="Customer pain point / friction")
    root_cause: str = Field("", description="Why traditional tools or manual methods fail")
    product_solution: str = Field(..., description="How our product solves it specifically")
    quantifiable_outcome: str = Field(..., description="Measurable metric outcome (e.g. 'Cuts manual data entry by 85%')")


class CaseStudyItem(BaseModel):
    client_name_or_industry: str = Field(..., description="Client name or industry description (e.g. 'Series B SaaS Startup', 'National Logistics Co.')")
    challenge: str = Field(..., description="What problem they faced before using our product")
    result_metric: str = Field(..., description="Measurable result (e.g. '3.2x pipeline increase in 60 days')")
    customer_quote: str | None = Field(None, description="Approved testimonial or executive quote")


class ValuePropROI(BaseModel):
    primary_tagline: str = Field(..., description="Main value hook")
    persona_messaging: list[PersonaMessaging] = Field(default_factory=list)
    pain_point_matrix: list[PainPointSolution] = Field(default_factory=list)
    roi_benchmarks: dict[str, str] = Field(
        default_factory=dict,
        description="Key ROI stats (e.g. {'average_payback_period': '45 days', 'cost_savings': '30%', 'hours_saved_per_rep': '12 hrs/week'})"
    )
    case_studies: list[CaseStudyItem] = Field(default_factory=list)


# ---------------------------------------------------------
# Pillar 4: Competitive Intelligence (Battlecards)
# ---------------------------------------------------------

class CompetitorBattlecard(BaseModel):
    competitor_name: str = Field(..., description="Competitor name")
    competitor_tier: str = Field("Direct", description="Direct, Legacy, or Alternative")
    their_weaknesses: list[str] = Field(default_factory=list, description="Where they fail (slow latency, high hidden fees, complex setup)")
    our_distinct_advantages: list[str] = Field(default_factory=list, description="Why we win over them")
    pricing_comparison: str = Field("", description="How their pricing compares (e.g. '2x more expensive with seat minimums')")
    killer_question_to_ask: str = Field(
        "",
        description="A provocative question the agent asks to highlight their flaw (e.g. 'Are you finding their 1.5-second latency causes awkward pauses on calls?')"
    )
    why_customers_switch: str = Field("", description="Common reason clients migrate from this competitor")


class DisplacementMigration(BaseModel):
    migration_timeline_days: str = Field("1-3 days", description="How fast a customer can switch")
    automated_importers: list[str] = Field(default_factory=list, description="Supported 1-click import tools")
    downtime_risk: str = Field("Zero downtime migration with parallel cutover", description="Risk mitigation statement")
    migration_support_included: str = Field("Dedicated migration specialist for all annual plans", description="Support level during switch")


class CompetitiveIntelligence(BaseModel):
    battlecards: list[CompetitorBattlecard] = Field(default_factory=list)
    displacement_strategy: DisplacementMigration = Field(default_factory=DisplacementMigration)


# ---------------------------------------------------------
# Pillar 5: Implementation, Onboarding & Support
# ---------------------------------------------------------

class ImplementationSupport(BaseModel):
    time_to_value_timeline: str = Field(
        "Self-serve setup in 15 minutes; full team onboarding within 5 business days.",
        description="Estimated timeline from contract sign to full productivity"
    )
    onboarding_milestones: list[str] = Field(
        default_factory=list,
        description="Step 1: Account setup, Step 2: Integration connect, Step 3: Team training, Step 4: Go-live"
    )
    customer_prerequisites: list[str] = Field(
        default_factory=list,
        description="What the customer needs ready (e.g. Admin access to CRM, phone list CSV)"
    )
    support_tiers: dict[str, str] = Field(
        default_factory=dict,
        description="Support SLA per plan (e.g. {'Starter': 'Email support (24h response)', 'Enterprise': '24/7 Phone + Slack channel + 1h SLA'})"
    )
    training_resources: list[str] = Field(
        default_factory=list,
        description="Knowledge base, weekly live webinars, dedicated customer success manager"
    )


# ---------------------------------------------------------
# Pillar 6: Security, Privacy & Compliance
# ---------------------------------------------------------

class SecurityCompliance(BaseModel):
    certifications: list[str] = Field(
        default_factory=list,
        description="SOC 2 Type II, ISO 27001, HIPAA, GDPR, CCPA, PCI-DSS compliant"
    )
    data_hosting_provider: str = Field("AWS (US-East / EU-Central)", description="Cloud hosting provider and regions")
    encryption_standards: str = Field(
        "AES-256 at rest, TLS 1.3 in transit. End-to-end encryption for voice streams.",
        description="Encryption methods"
    )
    data_retention_and_privacy: str = Field(
        "Customer data is never used to train shared public models. Zero-retention option available for Enterprise.",
        description="Privacy and LLM training policy"
    )
    uptime_sla_guarantee: str = Field("99.9% uptime SLA with financial service credits", description="Uptime guarantee")
    dpa_and_baa_available: bool = Field(True, description="Can we sign Data Processing Agreements and HIPAA BAAs?")


# ---------------------------------------------------------
# Pillar 7: Product Boundaries & Disqualifiers (Guardrails)
# ---------------------------------------------------------

class GuardrailsDisqualifiers(BaseModel):
    unsupported_features: list[str] = Field(
        default_factory=list,
        description="Explicit list of features the product DOES NOT have and CANNOT do"
    )
    out_of_scope_use_cases: list[str] = Field(
        default_factory=list,
        description="Scenarios where the product should NOT be sold"
    )
    disqualification_criteria: list[str] = Field(
        default_factory=list,
        description="Conditions where the agent must politely disqualify the lead (e.g. 'No budget (<$100)', 'Requires air-gapped on-premise without custom contract')"
    )
    polite_disqualification_script: str = Field(
        "Based on what you've described, our platform won't be the best fit for your exact setup right now. I don't want to waste your time—I'd recommend checking out [alternative category] instead. Thank you for your time today!",
        description="Exact response the agent uses when disqualifying"
    )


# ---------------------------------------------------------
# Complete Product Intelligence Container
# ---------------------------------------------------------

class ProductKnowledgeBase(BaseModel):
    product_name: str = Field(..., description="Official Product Name")
    company_name: str = Field(..., description="Company Name")
    website_url: str = Field(..., description="Main Website URL")
    tagline: str = Field("", description="One sentence high-impact summary")
    
    # The 7 Pillars
    core_specs: CoreSpecsCapabilities = Field(..., description="Pillar 1: Specs, features, technical limits")
    commercials_pricing: CommercialsPricing = Field(..., description="Pillar 2: Plans, pricing, discounts, terms")
    value_prop_roi: ValuePropROI = Field(..., description="Pillar 3: Personas, pain points, ROI, proof")
    competitive_intel: CompetitiveIntelligence = Field(..., description="Pillar 4: Battlecards and migration")
    implementation_support: ImplementationSupport = Field(..., description="Pillar 5: Onboarding, SLAs, training")
    security_compliance: SecurityCompliance = Field(..., description="Pillar 6: Certifications, privacy, uptime")
    guardrails_disqualifiers: GuardrailsDisqualifiers = Field(..., description="Pillar 7: Hard limits, disqualification rules")
