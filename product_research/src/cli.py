"""
Command-line Interface for the Autonomous AI Product Sales Intelligence Pipeline.
"""

import asyncio
import json
import os

import click

from .compiler.compiler import AgentCompiler
from urllib.parse import urlparse

from .crawler.market_crawler import gather_market_context
from .crawler.web_crawler import WebCrawler
from .extractor.sales_intelligence import extract_sales_intelligence
from .extractor.extractor import ProductExtractor
from .extractor.llm_client import LLMClient
from .sales_skills.gtm_playbooks import enrich_playbook_with_product_context
from .sales_skills.sales_psychology import get_core_sales_playbook
from .schemas.product_schema import ProductKnowledgeBase
from .utils.logger import console, print_banner, print_compilation_success, print_extraction_summary


@click.group()
def cli():
    """Autonomous AI Product Sales Intelligence Engine."""
    pass


@cli.command("build")
@click.option("--url", "-u", required=True, help="Website URL of the product to analyze.")
@click.option("--name", "-n", default=None, help="Optional product name hint.")
@click.option("--output-dir", "-o", default="./dist", help="Output directory for generated files.")
@click.option("--max-pages", "-m", default=25, help="Maximum number of subpages to crawl.")
@click.option("--provider", "-p", default="auto", type=click.Choice(["auto", "codex", "antigravity", "ollama", "openai", "gemini", "anthropic"]), help="LLM Provider.")
@click.option("--model", default=None, help="Model name (e.g. llama3.3, gpt-4o, gemini-2.5-flash, claude-3-5-sonnet).")
@click.option("--ollama-url", default="http://localhost:11434", help="Base URL for local Ollama instance.")
def build(url, name, output_dir, max_pages, provider, model, ollama_url):
    """End-to-end pipeline: Crawls site, extracts 7-pillar knowledge, and compiles 3-tier production artifacts."""
    print_banner()

    async def run_pipeline():
        # Step 1: Crawl Website
        console.print(f"[bold green]▶ Step 1/3:[/bold green] Crawling [cyan]{url}[/cyan] (Max {max_pages} pages)...")
        crawler = WebCrawler(max_pages=max_pages)
        pages = await crawler.crawl(url)
        console.print(f"  ✓ Successfully fetched and parsed [bold]{len(pages)}[/bold] pages.")
        
        aggregated_markdown = crawler.aggregate_markdown(pages)

        # Step 2: Extract 7-Pillar Product Knowledge & GTM Playbooks
        console.print(f"\n[bold green]▶ Step 2/3:[/bold green] Extracting 7-Pillar Product Knowledge using [yellow]{provider.upper()}[/yellow] ({model or 'default'})...")
        llm = LLMClient(provider=provider, model=model, ollama_base_url=ollama_url)
        extractor = ProductExtractor(llm_client=llm)

        kb = await extractor.extract_from_markdown(
            crawled_markdown=aggregated_markdown,
            website_url=url,
            product_name_hint=name
        )

        # How to sell it, not just what is true. Best-effort: a failure here
        # leaves the agent accurate but generic, which is still usable.
        console.print(
            "\n[bold green]▶ Step 2b/3:[/bold green] Learning how this product is sold..."
        )
        market_context = await gather_market_context(
            kb.product_name,
            own_domain=urlparse(url).netloc,
            category_hint=kb.core_specs.summary[:120] if kb.core_specs.summary else "",
        )
        sales_intel = await extract_sales_intelligence(
            llm,
            own_site=aggregated_markdown,
            market_context=market_context,
            product_name=kb.product_name,
        )
        console.print(
            f"  ✓ {len(sales_intel['objections'])} objection(s), "
            f"{len(sales_intel['sample_phrases'])} spoken example(s), "
            f"{len(sales_intel['discovery_questions'])} discovery question(s)"
        )

        base_playbook = get_core_sales_playbook(
            product_name=kb.product_name,
            primary_value_prop=kb.value_prop_roi.primary_tagline
        )
        enriched_playbook = enrich_playbook_with_product_context(
            playbook=base_playbook,
            product_name=kb.product_name,
            target_personas=kb.value_prop_roi.persona_messaging,
            discounts=kb.commercials_pricing.discount_matrix
        )

        print_extraction_summary(kb, enriched_playbook)

        # Step 3: Compile into 3-Tier Production Artifacts
        console.print(f"\n[bold green]▶ Step 3/3:[/bold green] Compiling 3-Tier Zero-Latency Voice Artifacts to [cyan]{output_dir}[/cyan]...")
        compiler = AgentCompiler(output_dir=output_dir)
        compiled_files = compiler.compile_all(kb, enriched_playbook)

        # Save the crawled source alongside the extraction. Without it there is
        # no way to verify later that a quoted price actually came from the site.
        os.makedirs(output_dir, exist_ok=True)
        source_path = os.path.join(output_dir, "crawled_source.md")
        with open(source_path, "w", encoding="utf-8") as f:
            f.write(aggregated_markdown)

        sales_intel_path = os.path.join(output_dir, "sales_intelligence.json")
        with open(sales_intel_path, "w", encoding="utf-8") as f:
            json.dump(sales_intel, f, indent=2, ensure_ascii=False)

        # Also save raw knowledge base JSON
        raw_kb_path = os.path.join(output_dir, "product_knowledge_base.json")
        with open(raw_kb_path, "w", encoding="utf-8") as f:
            f.write(kb.model_dump_json(indent=2))

        print_compilation_success(compiled_files)
        console.print("\n[bold green]✅ Production Voice Agent Knowledge Pack Successfully Generated![/bold green]\n")

    asyncio.run(run_pipeline())


@cli.command("crawl")
@click.option("--url", "-u", required=True, help="Website URL to crawl.")
@click.option("--output", "-o", default="./crawled_content.md", help="Path to save markdown.")
@click.option("--max-pages", "-m", default=25, help="Maximum pages.")
def crawl_cmd(url, output, max_pages):
    """Crawls website and exports cleaned markdown."""
    print_banner()

    async def run_crawl():
        console.print(f"[bold green]Crawling[/bold green] [cyan]{url}[/cyan]...")
        crawler = WebCrawler(max_pages=max_pages)
        pages = await crawler.crawl(url)
        markdown = crawler.aggregate_markdown(pages)
        with open(output, "w", encoding="utf-8") as f:
            f.write(markdown)
        console.print(f"[bold green]Saved {len(pages)} pages to {output}[/bold green]")

    asyncio.run(run_crawl())


@cli.command("compile")
@click.option("--input", "-i", "input_file", required=True, help="Path to product_knowledge_base.json")
@click.option("--output-dir", "-o", default="./dist", help="Output directory.")
def compile_cmd(input_file, output_dir):
    """Compiles an existing product knowledge JSON into the 3 tiers."""
    print_banner()
    with open(input_file, encoding="utf-8") as f:
        data = json.load(f)
    kb = ProductKnowledgeBase.model_validate(data)
    playbook = get_core_sales_playbook(kb.product_name, kb.value_prop_roi.primary_tagline)

    compiler = AgentCompiler(output_dir=output_dir)
    compiled = compiler.compile_all(kb, playbook)
    print_compilation_success(compiled)


@cli.command("ui")
@click.option("--host", default="127.0.0.1", help="Host address to bind.")
@click.option("--port", "-p", default=8000, help="Port to run the UI server.")
def ui_cmd(host, port):
    """Launches the interactive Web UI."""
    import uvicorn
    print_banner()
    console.print(f"[bold green]🌐 Starting Web UI at:[/bold green] [cyan]http://{host}:{port}[/cyan]\n")
    uvicorn.run("src.server:app", host=host, port=port, reload=False)

