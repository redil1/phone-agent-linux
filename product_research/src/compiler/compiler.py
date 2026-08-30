"""
3-Tier Production Agent Compiler & Packager.
Transforms 7-pillar product knowledge and sales playbooks into low-latency production files.
"""

import json
import os
from typing import Any

import yaml

from ..schemas.product_schema import ProductKnowledgeBase
from ..schemas.sales_skills_schema import SalesPlaybook


class AgentCompiler:
    def __init__(self, output_dir: str = "./dist"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def compile_all(
        self,
        kb: ProductKnowledgeBase,
        playbook: SalesPlaybook
    ) -> dict[str, str]:
        """Compiles all 3 tiers + the complete Voice Agent Prompt."""

        # 1. Tier 1: Hot In-Prompt YAML (0ms Latency)
        tier1_yaml = self.generate_hot_prompt_yaml(kb, playbook)
        tier1_path = os.path.join(self.output_dir, "hot_system_prompt.yaml")
        with open(tier1_path, "w", encoding="utf-8") as f:
            f.write(tier1_yaml)

        # 2. Tier 2: Fast Key-Value Store JSON (<15ms Latency)
        tier2_json = self.generate_fast_lookup_json(kb, playbook)
        tier2_path = os.path.join(self.output_dir, "fast_lookup.json")
        with open(tier2_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(tier2_json, indent=2, ensure_ascii=False))

        # 3. Tier 3: Edge Case Atomic Markdown KB (<50ms Latency)
        tier3_md = self.generate_edge_case_kb_md(kb)
        tier3_path = os.path.join(self.output_dir, "edge_case_kb.md")
        with open(tier3_path, "w", encoding="utf-8") as f:
            f.write(tier3_md)

        # 4. Master Production Voice Agent System Prompt
        voice_prompt = self.generate_voice_agent_system_prompt(kb, playbook, tier1_yaml)
        voice_prompt_path = os.path.join(self.output_dir, "voice_agent_prompt.txt")
        with open(voice_prompt_path, "w", encoding="utf-8") as f:
            f.write(voice_prompt)

        return {
            "tier1_hot_yaml": tier1_path,
            "tier2_fast_json": tier2_path,
            "tier3_edge_md": tier3_path,
            "voice_agent_prompt": voice_prompt_path
        }

    def generate_hot_prompt_yaml(self, kb: ProductKnowledgeBase, playbook: SalesPlaybook) -> str:
        """Generates minified, high-density YAML for live prompt context."""

        # Plans summary
        plans_dict = {}
        for p in kb.commercials_pricing.plans:
            plans_dict[p.name.lower().replace(" ", "_")] = {
                "monthly": p.price_monthly,
                "annual": p.price_annual,
                "billing": p.billing_unit,
                "best_for": p.best_for,
                "includes": p.included_features[:4],
                "excludes": p.excluded_features[:3]
            }

        # Discounts
        discounts_dict = {
            d.scenario: f"{d.max_discount_pct}% max ({d.conditions})"
            for d in kb.commercials_pricing.discount_matrix
        }

        # Battlecards
        competitors_dict = {}
        for b in kb.competitive_intel.battlecards:
            competitors_dict[b.competitor_name.lower().replace(" ", "_")] = {
                "their_flaw": b.their_weaknesses[:2],
                "our_advantage": b.our_distinct_advantages[:2],
                "killer_question": b.killer_question_to_ask
            }

        # Personas
        personas_dict = {
            p.role_title: p.tailored_pitch
            for p in kb.value_prop_roi.persona_messaging
        }

        # Objections key-value
        objections_dict = {}
        for obj in playbook.objection_library:
            objections_dict[obj.category.lower().replace(" ", "_")] = {
                "acknowledge": obj.step_1_acknowledge,
                "isolate": obj.step_2_reframe_isolate,
                "bridge": obj.step_3_value_bridge,
                "trial_close": obj.step_4_trial_close
            }

        hot_data = {
            "product": {
                "name": kb.product_name,
                "tagline": kb.tagline,
                "url": kb.website_url,
                "summary": kb.core_specs.summary
            },
            "commercials": {
                "plans": plans_dict,
                "discount_rules": discounts_dict,
                "hard_margin_ceiling": kb.commercials_pricing.hard_margin_floor,
                "trial": kb.commercials_pricing.trial_policy,
                "refund_policy": kb.commercials_pricing.cancellation_and_refund_policy
            },
            "personas": personas_dict,
            "roi_benchmarks": kb.value_prop_roi.roi_benchmarks,
            "competitor_battlecards": competitors_dict,
            "guardrails": {
                "unsupported_features": kb.guardrails_disqualifiers.unsupported_features,
                "disqualification_criteria": kb.guardrails_disqualifiers.disqualification_criteria,
                "disqualify_script": kb.guardrails_disqualifiers.polite_disqualification_script
            },
            "objection_rebuttals": objections_dict,
            "voice_pacing": {
                "target_wpm": playbook.voice_dynamics.speaking_rate_wpm,
                "barge_in_rule": playbook.voice_dynamics.barge_in_handling_rule
            }
        }

        return yaml.dump(hot_data, sort_keys=False, allow_unicode=True)

    def generate_fast_lookup_json(self, kb: ProductKnowledgeBase, playbook: SalesPlaybook) -> dict[str, Any]:
        """Generates key-value indexed JSON for fast exact tool retrieval (<15ms)."""
        lookup = {
            "metadata": {
                "product_name": kb.product_name,
                "company_name": kb.company_name,
                "website_url": kb.website_url
            },
            "features": {},
            "plans": {},
            "competitors": {},
            "case_studies": {},
            "security": {
                "certifications": kb.security_compliance.certifications,
                "hosting": kb.security_compliance.data_hosting_provider,
                "encryption": kb.security_compliance.encryption_standards,
                "privacy": kb.security_compliance.data_retention_and_privacy,
                "uptime_sla": kb.security_compliance.uptime_sla_guarantee,
                "dpa_baa": kb.security_compliance.dpa_and_baa_available
            },
            "support": {
                "timeline": kb.implementation_support.time_to_value_timeline,
                "milestones": kb.implementation_support.onboarding_milestones,
                "prerequisites": kb.implementation_support.customer_prerequisites,
                "tiers": kb.implementation_support.support_tiers
            },
            "discovery_questions": [q.model_dump() for q in playbook.discovery_questions],
            "closing_techniques": [c.model_dump() for c in playbook.closing_frameworks],
            "escalation_rules": playbook.escalation_rules.model_dump()
        }

        for f in kb.core_specs.features:
            slug = f.name.lower().replace(" ", "_").replace("/", "_")
            lookup["features"][slug] = f.model_dump()

        for p in kb.commercials_pricing.plans:
            slug = p.name.lower().replace(" ", "_")
            lookup["plans"][slug] = p.model_dump()

        for b in kb.competitive_intel.battlecards:
            slug = b.competitor_name.lower().replace(" ", "_")
            lookup["competitors"][slug] = b.model_dump()

        for idx, cs in enumerate(kb.value_prop_roi.case_studies):
            slug = f"case_study_{idx + 1}"
            lookup["case_studies"][slug] = cs.model_dump()

        return lookup

    def generate_edge_case_kb_md(self, kb: ProductKnowledgeBase) -> str:
        """Generates atomic markdown micro-chunks with YAML frontmatter."""
        chunks = []

        # 1. Security & Compliance
        chunks.append(f"""---
category: security_compliance
topic: certifications_and_data_privacy
tags: [soc2, gdpr, hipaa, encryption, sla]
---
# Security, Compliance & Data Privacy
* **Certifications:** {', '.join(kb.security_compliance.certifications)}
* **Hosting & Region:** {kb.security_compliance.data_hosting_provider}
* **Encryption Standards:** {kb.security_compliance.encryption_standards}
* **Data Privacy Policy:** {kb.security_compliance.data_retention_and_privacy}
* **Uptime Guarantee:** {kb.security_compliance.uptime_sla_guarantee}
* **BAA & DPA:** {"Available upon request" if kb.security_compliance.dpa_and_baa_available else "Not supported"}
""")

        # 2. Migration & Displacement
        chunks.append(f"""---
category: implementation
topic: migration_and_switching
tags: [migration, switching, downtime, onboarding]
---
# Competitor Migration & Switching Guide
* **Migration Timeline:** {kb.competitive_intel.displacement_strategy.migration_timeline_days}
* **Automated Importers:** {', '.join(kb.competitive_intel.displacement_strategy.automated_importers)}
* **Downtime Risk:** {kb.competitive_intel.displacement_strategy.downtime_risk}
* **Dedicated Support:** {kb.competitive_intel.displacement_strategy.migration_support_included}
""")

        # 3. Technical Specs & Limits
        chunks.append(f"""---
category: technical_specs
topic: platform_requirements_and_limits
tags: [api, webhooks, rate_limits, platforms]
---
# Technical Architecture & API Specifications
* **Architecture:** {kb.core_specs.technical_specs.architecture_overview}
* **Supported Platforms:** {', '.join(kb.core_specs.technical_specs.supported_platforms)}
* **System Requirements:** {', '.join(kb.core_specs.technical_specs.system_requirements)}
* **API Capabilities:** {kb.core_specs.integrations.api_capabilities}
* **Supported Webhooks:** {', '.join(kb.core_specs.integrations.webhook_events)}
* **Setup Complexity:** {kb.core_specs.integrations.setup_complexity}
""")

        # 4. Disqualification Guardrails
        chunks.append(f"""---
category: guardrails
topic: unsupported_features_and_disqualification
tags: [out_of_scope, unsupported, disqualification]
---
# Hard Guardrails & Disqualification Rules
* **Unsupported Capabilities:**
{chr(10).join(f'  - {item}' for item in kb.guardrails_disqualifiers.unsupported_features)}
* **Out-of-Scope Use Cases:**
{chr(10).join(f'  - {item}' for item in kb.guardrails_disqualifiers.out_of_scope_use_cases)}
* **Disqualification Criteria:**
{chr(10).join(f'  - {item}' for item in kb.guardrails_disqualifiers.disqualification_criteria)}
""")

        return "\n\n<!-- slide -->\n\n".join(chunks)

    def generate_voice_agent_system_prompt(
        self,
        kb: ProductKnowledgeBase,
        playbook: SalesPlaybook,
        hot_yaml: str
    ) -> str:
        """Generates the master production prompt for autonomous real-time voice phone calls."""

        prompt = f"""# AUTONOMOUS PHONE SALES AGENT - REAL-TIME VOICE SYSTEM PROMPT

## 1. IDENTITY & MISSION
You are Alex, an elite Senior Solutions Director & Commercial Sales Representative for {kb.product_name}.
Your mission is to conduct high-conversion, consultative outbound/inbound phone calls with prospective decision-makers.
You are articulate, sharp, empathetic, confident, and 100% fluent in every technical and commercial aspect of {kb.product_name}.

## 2. REAL-TIME CONVERSATIONAL VOICE RULES (CRITICAL)
1. **Ultra-Concise Turns:** Speak in short, conversational sentences (1-3 sentences per turn maximum). Never lecture or monologue.
2. **Pacing & Cadence:** Maintain a natural speaking rate (~{playbook.voice_dynamics.speaking_rate_wpm} WPM). Use natural conversational bridges ("Got it", "Totally understand", "Makes sense").
3. **Barge-In / Interruption Handling:** {playbook.voice_dynamics.barge_in_handling_rule}
4. **Never Sound Like a Bot:** Do not use phrases like "Based on my data", "As an AI", or "I have processed your request". Talk like a seasoned high-performing human account executive.

## 3. CONVERSATION STATE MACHINE

### STAGE 1: THE OPENING HOOK (0 - 30 seconds)
* Confirm you are speaking with the decision-maker.
* Deliver a 1-sentence value hook tailored to their role.
* Example: "Hi [Prospect Name], this is Alex from {kb.product_name}. I saw your team is expanding operations and wanted to see how you're currently tackling [core pain point]?"

### STAGE 2: DISCOVERY & PAIN ISOLATION (MEDDPICC)
* Ask open-ended questions to uncover friction, manual overhead, or tool dissatisfaction.
* Probe for quantifiable metrics (hours lost, revenue impact, latency issues).
* Validate their pain with genuine empathy before pitching solutions.

### STAGE 3: VALUE BRIDGE & ROI PRESENTATION
* Map their exact pain directly to the corresponding {kb.product_name} feature.
* State verifiable ROI proof (e.g. "{kb.value_prop_roi.roi_benchmarks.get('hours_saved_per_rep', '10+ hours/week')} saved, {kb.value_prop_roi.roi_benchmarks.get('average_payback_period', '45-day payback')}").
* Use contrast against competitors where relevant.

### STAGE 4: OBJECTION HANDLING (THE 4-STEP LOOP)
When faced with an objection (Price, Timing, Competitor, Skepticism):
1. **Acknowledge:** Validate their concern sincerely.
2. **Isolate:** Check if there are other blocking factors.
3. **Value Bridge:** Deliver concrete evidence / guarantee.
4. **Trial Close:** Propose a risk-free next step (14-day trial or 15-min specialist walkthrough).

### STAGE 5: THE ASSUMPTIVE CLOSE & CALENDAR LOCK
* Use the Two-Option Close: "Would Tuesday morning or Thursday afternoon work better for a 15-minute screen-share?"
* Or trigger SMS activation link: "I can text you the 1-click activation link right now while we're on the phone."

### STAGE 6: DISQUALIFICATION OR WARM HANDOFF
* If the prospect does not meet minimum qualification criteria, use the polite disqualification script:
  "{kb.guardrails_disqualifiers.polite_disqualification_script}"
* If deal requires complex enterprise MSA (> $25k) or custom SLA, trigger human transfer:
  "{playbook.escalation_rules.transfer_script}"

---

## 4. IN-PROMPT KNOWLEDGE BASE (TIER 1 HOT DATA - 0ms LATENCY)
```yaml
{hot_yaml}
```

## 5. AVAILABLE LIVE TOOLS
* `check_calendar_availability(date_range)`: Query live open slots for specialist demo.
* `send_sms_collateral(phone_number, link_type)`: Send 1-click trial link, product deck, or checkout link mid-call.
* `initiate_warm_transfer(phone_number, reason)`: Transfer call to Senior Human Executive.
* `log_call_disposition(status, notes, meddpicc_score)`: Save structured notes to CRM immediately.
"""
        return prompt.strip()
