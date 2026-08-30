# ⚡ Autonomous AI Sales Product Intelligence Pipeline

A production-ready pipeline that crawls any product website, extracts comprehensive **7-Pillar Product Knowledge**, synthesizes top-tier **GTM Sales Psychology** (from `zarif3624/gtm-skills`, `louisblythe/sales-skills`, and `chadboyda/agent-gtm-skills`), and compiles them into **zero-latency 3-Tier Production Artifacts** for autonomous phone sales AI agents.

---

## 🎯 Architecture Overview

```
                      ┌────────────────────────┐
                      │   Target Website URL   │
                      └───────────┬────────────┘
                                  │
                                  ▼
                  ┌─────────────────────────────────┐
                  │    1. Multi-Engine Web Crawl    │
                  │  (Sitemap, Deep Pages, Docs)   │
                  └───────────────┬─────────────────┘
                                  │ Clean Markdown
                                  ▼
                  ┌─────────────────────────────────┐
                  │   2. 7-Pillar Schema Extractor  │
                  │  (Strict Pydantic Validation)   │
                  └───────────────┬─────────────────┘
                                  │ Structured Product Data
                                  ▼
┌───────────────────────────┐     │
│  GTM & Sales Skills Pack  │────►│
│ (zarif3624, louisblythe,  │     │
│   chadboyda frameworks)   │     ▼
└───────────────────────────┘ ┌─────────────────────────────────┐
                              │   3. Agent Compiler & Packager  │
                              └───────────────┬─────────────────┘
                                              │
      ┌───────────────────────────────────────┼───────────────────────────────────────┐
      ▼                                       ▼                                       ▼
┌───────────────────┐               ┌───────────────────┐                   ┌───────────────────┐
│ Tier 1: Hot Prompt│               │ Tier 2: Fast Cache│                   │ Tier 3: Edge Docs │
│   (minified YAML) │               │   (Indexed JSON)  │                   │  (Atomic MD KB)   │
│    [0ms Latency]  │               │   [<15ms Latency] │                   │   [<50ms Latency] │
└───────────────────┘               └───────────────────┘                   └───────────────────┘
      │                                       │                                       │
      └───────────────────────────────────────┼───────────────────────────────────────┘
                                              ▼
                            ┌───────────────────────────────────┐
                            │ Ready-to-Deploy Voice System      │
                            │ Prompt & State Machine Script     │
                            └───────────────────────────────────┘
```

---

## 📦 The 7 Product Knowledge Pillars

1. **Core Specs & Capabilities:** Full feature catalog, technical requirements, platform limits, API & webhook integrations, and release/roadmap guardrails.
2. **Commercials, Pricing & Packaging:** Plans breakdown, seat metrics, pre-approved discount matrix, margin floor rules, trial policies, and refund terms.
3. **Value Proposition & ROI Data:** Role-tailored pitches (CEO vs CTO vs VP Sales), pain-point-to-solution mappings, verifiable ROI benchmarks, and case studies.
4. **Competitive Intelligence (Battlecards):** Feature comparisons, competitor weaknesses, "why customers switch", displacement strategies, and killer discovery questions.
5. **Implementation & Support SLAs:** Time-to-value milestones, customer onboarding prerequisites, support tiers (24/7, SLAs), and training materials.
6. **Security, Privacy & Compliance:** SOC 2 Type II, GDPR, HIPAA BAA availability, encryption standards, hosting infrastructure, and uptime guarantees.
7. **Guardrails & Disqualifiers:** What the product *cannot* do, out-of-scope use cases, disqualification criteria, and polite deflection scripts.

---

## 🧠 Integrated GTM & Sales Skills

Synthesizes frameworks from top open-source sales skill collections:
* **`zarif3624/gtm-skills`**: Evidence-based ICP definition, discovery questioning, MEDDPICC qualification (Metrics, Economic Buyer, Decision Criteria, Decision Process, Pain, Champion, Competition).
* **`louisblythe/sales-skills`**: Conversational voice dynamics (140-155 WPM), active listening cues, 4-step objection handling loop (Acknowledge $\to$ Isolate $\to$ Value Bridge $\to$ Trial Close), assumptive two-option calendar locking.
* **`chadboyda/agent-gtm-skills`**: Signal-based intent routing, mid-call SMS collateral triggers, and enterprise human warm-transfer thresholds.

---

## 🚀 Quickstart

### 1. Installation

```bash
cd ProductSearchEngine
pip install -r requirements.txt
```

### 2. Run End-to-End Build

#### With Local Ollama (100% Private / Local Models):
```bash
# Ensure Ollama is running (e.g. ollama run llama3.3)
python main.py build --url https://yourproduct.com --provider ollama --model llama3.3 --output-dir ./dist
```

#### With OpenAI (`gpt-4o`):
```bash
export OPENAI_API_KEY="sk-..."
python main.py build --url https://yourproduct.com --provider openai --model gpt-4o --output-dir ./dist
```

#### With Google Gemini (`gemini-2.5-flash`):
```bash
export GEMINI_API_KEY="..."
python main.py build --url https://yourproduct.com --provider gemini --model gemini-2.5-flash --output-dir ./dist
```

#### With Anthropic (`claude-3-5-sonnet`):
```bash
export ANTHROPIC_API_KEY="..."
python main.py build --url https://yourproduct.com --provider anthropic --output-dir ./dist
```

#### Offline / Zero-API Key Mode:
```bash
python main.py build --url https://github.com/features --output-dir ./dist
```

---

## 📂 Output Artifacts (`./dist`)

* **`hot_system_prompt.yaml`**: Minified, token-budgeted (<3.5k tokens) YAML context injected into the system prompt for **0ms sub-second voice reflexes**.
* **`fast_lookup.json`**: Key-value hashed JSON for instant (<15ms) in-memory Redis or tool function lookups.
* **`edge_case_kb.md`**: Atomic micro-chunks formatted in markdown with YAML frontmatter for compliance, security, and edge-case queries.
* **`voice_agent_prompt.txt`**: Production-ready master system prompt with the complete conversational state machine, barge-in rules, and hot product knowledge.
* **`product_knowledge_base.json`**: Complete validated Pydantic JSON schema.

---

## 🧪 Testing

Run the automated test suite:
```bash
pytest tests/ -v
```
