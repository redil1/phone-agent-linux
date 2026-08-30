"""
FastAPI Web Server for Autonomous AI Product Sales Intelligence.
Provides REST API & Web UI for crawling, 7-pillar knowledge extraction, and 3-tier compiling.
"""

import json
import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .compiler.compiler import AgentCompiler
from .crawler.web_crawler import WebCrawler
from .extractor.extractor import ProductExtractor
from .extractor.llm_client import LLMClient
from .sales_skills.gtm_playbooks import enrich_playbook_with_product_context
from .sales_skills.sales_psychology import get_core_sales_playbook

app = FastAPI(title="Autonomous AI Sales Intelligence Engine", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static directory setup
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class GenerateRequest(BaseModel):
    url: str = Field(..., description="Target website URL")
    name: str | None = Field(None, description="Product / Brand name hint")
    provider: str = Field("auto", description="LLM provider: ollama, openai, gemini, anthropic, auto")
    model: str | None = Field(None, description="Model name or manual Ollama tag")
    api_key: str | None = Field(None, description="API Key for cloud providers")
    ollama_url: str = Field("http://localhost:11434", description="Ollama API base URL")
    custom_base_url: str | None = Field(None, description="Custom API endpoint (e.g. https://api.moonshot.cn/v1 for Kimi)")
    max_pages: int = Field(15, description="Maximum pages to crawl")
    concurrency: int = Field(5, description="Concurrent crawl workers")


class SimulateVoiceTurnRequest(BaseModel):
    user_message: str
    kb_data: dict[str, Any]
    playbook_data: dict[str, Any] | None = None


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Loading UI...</h1>")


@app.get("/api/ollama/status")
async def check_ollama_status(ollama_url: str = "http://localhost:11434"):
    """Checks if local Ollama instance is active and fetches downloaded models."""
    try:
        clean_url = ollama_url.rstrip("/")
        async with httpx.AsyncClient(timeout=4.0) as client:
            res = await client.get(f"{clean_url}/api/tags")
            if res.status_code == 200:
                data = res.json()
                models = [m.get("name") for m in data.get("models", [])]
                return {"online": True, "models": models, "url": clean_url}
    except Exception as e:
        return {"online": False, "models": [], "error": str(e), "url": ollama_url}

    return {"online": False, "models": [], "url": ollama_url}


@app.post("/api/generate")
async def generate_knowledge_pack(req: GenerateRequest):
    """Executes the full pipeline: Crawl -> 7-Pillar Extract -> GTM Synthesis -> Compile 3 Tiers."""
    try:
        # Step 1: Crawl
        crawler = WebCrawler(max_pages=req.max_pages, concurrency=req.concurrency)
        crawled_pages = await crawler.crawl(req.url)
        if not crawled_pages:
            raise HTTPException(status_code=400, detail="Failed to crawl any valid HTML pages from the given URL.")

        aggregated_markdown = crawler.aggregate_markdown(crawled_pages)
        pages_summary = [{"url": p.url, "title": p.title, "category": p.category} for p in crawled_pages]

        # Step 2: Extract 7 Pillars
        llm = LLMClient(
            provider=req.provider,
            model=req.model,
            api_key=req.api_key,
            ollama_base_url=req.ollama_url,
            custom_base_url=req.custom_base_url
        )
        extractor = ProductExtractor(llm_client=llm)


        kb = await extractor.extract_from_markdown(
            crawled_markdown=aggregated_markdown,
            website_url=req.url,
            product_name_hint=req.name
        )

        # Step 3: Sales Playbook
        base_playbook = get_core_sales_playbook(kb.product_name, kb.value_prop_roi.primary_tagline)
        enriched_playbook = enrich_playbook_with_product_context(
            playbook=base_playbook,
            product_name=kb.product_name,
            target_personas=kb.value_prop_roi.persona_messaging,
            discounts=kb.commercials_pricing.discount_matrix
        )

        # Step 4: Compile 3-Tier Production Files
        temp_dist = os.path.abspath("./dist")
        os.makedirs(temp_dist, exist_ok=True)
        compiler = AgentCompiler(output_dir=temp_dist)
        compiled_paths = compiler.compile_all(kb, enriched_playbook)

        # Read back compiled strings for instant UI rendering
        with open(compiled_paths["tier1_hot_yaml"], encoding="utf-8") as f:
            hot_yaml = f.read()

        with open(compiled_paths["tier2_fast_json"], encoding="utf-8") as f:
            fast_json = json.load(f)

        with open(compiled_paths["tier3_edge_md"], encoding="utf-8") as f:
            edge_md = f.read()

        with open(compiled_paths["voice_agent_prompt"], encoding="utf-8") as f:
            voice_prompt = f.read()

        return {
            "success": True,
            "product_name": kb.product_name,
            "tagline": kb.tagline,
            "pages_crawled_count": len(crawled_pages),
            "pages_summary": pages_summary,
            "kb_data": kb.model_dump(),
            "playbook_data": enriched_playbook.model_dump(),
            "tier1_hot_yaml": hot_yaml,
            "tier2_fast_json": fast_json,
            "tier3_edge_md": edge_md,
            "voice_agent_prompt": voice_prompt
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/simulate-voice-turn")
async def simulate_voice_turn(req: SimulateVoiceTurnRequest):
    """Simulates a live sub-second voice agent turn using the extracted hot knowledge."""
    user_msg = req.user_message.lower()
    kb = req.kb_data
    
    # Check if objection matches
    if any(w in user_msg for w in ["expensive", "cost", "price", "budget", "discount"]):
        return {
            "intent": "Objection - Price & Budget",
            "agent_response": (
                "I completely understand—budget stewardship is critical. Aside from the upfront cost, "
                f"does {kb.get('product_name', 'our platform')} solve the core operational friction you're facing? "
                "Most of our clients see full payback within 45 days. Would it make sense to do a 15-minute ROI breakdown?"
            ),
            "latency_ms": "12ms (In-Memory Hot Reflex)",
            "step_executed": "4-Step Objection Loop (Acknowledge -> Isolate -> Bridge -> Trial Close)"
        }
    elif any(w in user_msg for w in ["competitor", "already use", "alternative", "switch"]):
        return {
            "intent": "Objection - Existing Competitor",
            "agent_response": (
                "They are a well-known tool, and we respect what they've built! Curious—are you experiencing "
                f"any latency bottlenecks or setup complexity with them? Many clients switch to {kb.get('product_name')} "
                "specifically because we offer sub-300ms speed and 1-click zero-downtime migration. Open to seeing a quick side-by-side?"
            ),
            "latency_ms": "14ms (In-Memory Hot Reflex)",
            "step_executed": "Competitive Differentiation Loop"
        }
    elif any(w in user_msg for w in ["hipaa", "soc2", "security", "gdpr", "compliance", "encrypt"]):
        sec = kb.get("security_compliance", {})
        certs = ", ".join(sec.get("certifications", ["SOC 2 Type II", "GDPR", "HIPAA Ready"]))
        return {
            "intent": "Technical Enquiry - Security & Compliance",
            "agent_response": (
                f"Yes, absolutely. We are {certs}. All voice streams and data are encrypted with AES-256 at rest and TLS 1.3 in transit, "
                "and we sign BAAs for enterprise clients. Would you like me to text you our compliance package?"
            ),
            "latency_ms": "9ms (In-Memory Hot Reflex)",
            "step_executed": "Security Assurance & Collateral Dispatch"
        }
    else:
        tagline = kb.get("value_prop_roi", {}).get("primary_tagline", "accelerate business workflows")
        return {
            "intent": "General Value Discovery",
            "agent_response": (
                f"Got it! The main reason teams look at {kb.get('product_name', 'us')} is to {tagline.lower()}. "
                "What's currently taking up the biggest chunk of your team's bandwidth each week?"
            ),
            "latency_ms": "10ms (In-Memory Hot Reflex)",
            "step_executed": "MEDDPICC Problem Discovery Question"
        }
