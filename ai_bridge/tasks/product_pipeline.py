"""Run the product research engine and import its result, for the Studio.

The Studio hands over a website URL. This crawls it, extracts the seven pillars,
verifies every claim against the crawled source, and returns a report the
operator can act on. Nothing is written unless the verification gate passes and
activation is asked for.

The engine ships in ``product_research/`` but is driven as a subprocess rather
than imported, so its dependencies stay out of the call runtime's import graph
and a crash there cannot take a live call down with it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .product_import import ImportReport, activate, import_product
from .task_engine import TaskEngine
from .tool_registry import load_user_tools, registered_tools

logger = logging.getLogger("ProductPipeline")

ENGINE_DIR_ENV = "PHONE_AGENT_PRODUCT_ENGINE_DIR"
# The research engine ships inside this project so a checkout is one product.
# In source checkouts, parents[2] is the root repo. When installed as package phone_agent_gateway,
# parents[3] is the /app root or repo root.
_repo_candidates = [
    Path(__file__).resolve().parents[2] / "product_research",
    Path(__file__).resolve().parents[3] / "product_research" if len(Path(__file__).resolve().parents) > 3 else Path("/nonexistent"),
    Path("/app/product_research"),
]
BUNDLED_ENGINE_DIR = next((c for c in _repo_candidates if (c / "main.py").is_file()), _repo_candidates[0])
# Kept as a fallback for a standalone checkout of the engine beside the gateway.
LEGACY_ENGINE_DIR = Path.home() / "Desktop" / "ProductSearchEngine"
# A crawl plus a full seven-pillar extraction is slow, but not this slow.
BUILD_TIMEOUT_SECS = 900.0
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)

ProgressSink = Callable[[str], Awaitable[None] | None]


class ProductPipelineError(RuntimeError):
    """The research engine could not produce a knowledge base."""


def engine_dir() -> Path:
    """Where the product research engine lives, most specific first."""

    configured = os.getenv(ENGINE_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    if (BUNDLED_ENGINE_DIR / "main.py").is_file():
        return BUNDLED_ENGINE_DIR
    return LEGACY_ENGINE_DIR


def engine_available() -> bool:
    return (engine_dir() / "main.py").is_file()


# Every provider the engine can drive, with the models each is known to serve.
# Listed whether or not it is reachable right now: the operator picks, and an
# unreachable one is labelled rather than hidden, so a missing login is visible
# instead of a provider silently absent from the menu.
KNOWN_PROVIDERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("codex", "Codex — your ChatGPT plan", ("gpt-5.4-mini", "gpt-5.4", "gpt-5.5")),
    ("antigravity", "Gemini — your Antigravity login", ("gemini-3.7-flash-tiered",)),
    ("ollama", "Ollama — local models", ()),
    ("gemini", "Gemini — API key", ("gemini-2.5-flash", "gemini-2.5-pro")),
    ("openai", "OpenAI — API key", ("gpt-4o", "gpt-4o-mini")),
    ("anthropic", "Anthropic — API key", ("claude-3-5-sonnet-20241022",)),
)
API_KEY_ENV: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
}


def _ollama_models() -> list[str]:
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        response = httpx.get(f"{base}/api/tags", timeout=3.0)
        if response.status_code != 200:
            return []
        names = [m.get("name", "") for m in response.json().get("models", []) if m.get("name")]
    except Exception:
        logger.info("Ollama is not reachable at %s", base)
        return []
    # Local models first: a cloud tag still needs an Ollama account, and the
    # marker sits in the tag as either ":cloud" or "-cloud".
    return sorted(names, key=lambda n: ("cloud" in n.rsplit(":", 1)[-1], n))


def _subscription_models() -> dict[str, list[str]]:
    try:
        import sys as _sys

        engine_src = str(engine_dir())
        if engine_src not in _sys.path:
            _sys.path.insert(0, engine_src)
        from src.extractor.subscription_providers import available_subscription_providers

        return dict(available_subscription_providers())
    except Exception:
        logger.info("No subscription-backed extraction providers are available")
        return {}


def available_extraction_models() -> dict[str, dict[str, Any]]:
    """Every provider, its models, and whether it can be used right now."""

    subscriptions = _subscription_models()
    ollama = _ollama_models()
    providers: dict[str, dict[str, Any]] = {}

    for name, label, defaults in KNOWN_PROVIDERS:
        if name in ("codex", "antigravity"):
            models = subscriptions.get(name) or list(defaults)
            available = name in subscriptions
            reason = "" if available else "not signed in on this machine"
        elif name == "ollama":
            models = ollama
            available = bool(ollama)
            reason = "" if available else "Ollama is not running, or has no models pulled"
        else:
            keys = API_KEY_ENV[name]
            available = any(os.getenv(key, "").strip() for key in keys)
            models = list(defaults)
            reason = "" if available else f"set {keys[0]}"
        providers[name] = {
            "label": label,
            "models": models,
            "available": available,
            "reason": reason,
        }
    return providers


async def _emit(progress: ProgressSink | None, message: str) -> None:
    logger.info("%s", message)
    if progress is None:
        return
    result = progress(message)
    if asyncio.iscoroutine(result):
        await result


async def research_product(
    url: str,
    output_dir: Path,
    *,
    max_pages: int = 25,
    provider: str = "auto",
    model: str | None = None,
    progress: ProgressSink | None = None,
) -> Path:
    """Crawl and extract one product, returning the engine's output directory."""

    if not URL_RE.match(url.strip()):
        raise ProductPipelineError(f"{url!r} is not a valid http(s) URL")
    root = engine_dir()
    if not (root / "main.py").is_file():
        raise ProductPipelineError(
            f"The product research engine was not found at {root}. "
            f"Set {ENGINE_DIR_ENV} to its directory."
        )

    command = [
        sys.executable, "main.py", "build",
        "--url", url.strip(),
        "--output-dir", str(output_dir),
        "--max-pages", str(max_pages),
        "--provider", provider,
    ]
    if model:
        command += ["--model", model]

    await _emit(progress, f"Crawling {url} and extracting the seven pillars…")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    tail: list[str] = []
    assert process.stdout is not None
    try:
        async with asyncio.timeout(BUILD_TIMEOUT_SECS):
            while line := await process.stdout.readline():
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                tail.append(text)
                del tail[:-40]
                if any(marker in text for marker in ("Step", "✓", "pages", "Error", "error")):
                    await _emit(progress, text)
            await process.wait()
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ProductPipelineError(
            f"The research engine did not finish within {BUILD_TIMEOUT_SECS:.0f}s"
        ) from exc

    if process.returncode != 0:
        raise ProductPipelineError(
            "The research engine failed:\n" + "\n".join(tail[-12:])
        )
    for required in ("product_knowledge_base.json", "crawled_source.md"):
        if not (output_dir / required).is_file():
            raise ProductPipelineError(
                f"The research engine produced no {required}; nothing can be verified."
            )
    return output_dir


async def build_task_from_url(
    url: str,
    *,
    task_id: str,
    agent_name: str = "Adam",
    max_pages: int = 25,
    provider: str = "auto",
    model: str | None = None,
    activate_when_clean: bool = False,
    strict: bool = False,
    engine: TaskEngine | None = None,
    progress: ProgressSink | None = None,
) -> tuple[ImportReport, bool]:
    """Research a product and turn it into a verified task contract.

    Returns the report and whether the contract was actually activated. A report
    with blocking problems never activates, whatever was asked for.
    """

    workspace = Path(tempfile.mkdtemp(prefix="phone-agent-product-"))
    try:
        await research_product(
            url, workspace, max_pages=max_pages, provider=provider,
            model=model, progress=progress,
        )
        await _emit(progress, "Verifying every claim against the crawled source…")
        load_user_tools()
        report = await asyncio.to_thread(
            import_product,
            workspace / "product_knowledge_base.json",
            workspace / "crawled_source.md",
            task_id=task_id,
            sales_intelligence_path=workspace / "sales_intelligence.json",
            agent_name=agent_name,
            implemented_tools=set(registered_tools()),
            strict=strict,
        )
        await _emit(progress, report.summary())

        activated = False
        if activate_when_clean:
            activated = await asyncio.to_thread(activate, report, engine)
            await _emit(
                progress,
                f"Activated {task_id}." if activated
                else "Not activated: unverified claims must be resolved first.",
            )
        return report, activated
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def report_payload(report: ImportReport, activated: bool) -> dict:
    """Shape a report for the Studio."""

    return {
        "task_id": report.task_id,
        "product_name": report.product_name,
        "can_auto_apply": report.can_auto_apply,
        "activated": activated,
        "summary": report.summary(),
        "verified": [{"topic": c.topic, "fact": c.fact} for c in report.accepted],
        "dropped": [{"topic": c.topic, "reason": c.reason} for c in report.rejected],
        "blocking": list(report.blocking),
        "warnings": list(report.warnings),
        "knowledge_count": len(report.contract.get("knowledge", {})),
        "allowed_tools": list(report.contract.get("allowed_tools", [])),
        "selected": False,
    }
