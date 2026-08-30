"""Studio-driven product research: nothing unverified may reach a live task."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from phone_agent_gateway.ai_bridge.tasks import product_pipeline
from phone_agent_gateway.ai_bridge.tasks.product_pipeline import (
    ProductPipelineError,
    build_task_from_url,
    engine_available,
    engine_dir,
    report_payload,
    research_product,
)
from phone_agent_gateway.ai_bridge.tasks.task_engine import TaskEngine

SOURCE = (
    ("Streamly brings your programmes together in one place. " * 12)
    + "\nThe Advanced plan is $59 for 12 months.\n"
    + ("Streamly brings your programmes together in one place. " * 12)
    + "\nWe are SOC2 certified with 99.9% uptime. Setup takes about 10 minutes.\n"
    + ("Streamly brings your programmes together in one place. " * 12)
)

KB = {
    "product_name": "Streamly",
    "company_name": "Streamly Media",
    "core_specs": {"summary": "Streamly is a streaming platform.", "features": []},
    "commercials_pricing": {
        "plans": [{"name": "Advanced", "price_monthly": "$59", "billing_unit": "12 months"}],
        "trial_policy": "Setup takes about 10 minutes.",
    },
    "value_prop_roi": {"primary_tagline": "Watch everything", "persona_messaging": []},
    "competitive_intel": {"battlecards": []},
    "implementation_support": {},
    "security_compliance": {
        "certifications": ["SOC2"],
        "uptime_guarantee": "99.9% uptime",
    },
    "guardrails_disqualifiers": {},
}


def fake_engine_output(
    directory: Path, knowledge_base: dict | None = None, source: str = SOURCE
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "product_knowledge_base.json").write_text(
        json.dumps(knowledge_base or KB), encoding="utf-8"
    )
    (directory / "crawled_source.md").write_text(source, encoding="utf-8")


@pytest.fixture
def stub_research(monkeypatch):
    """Stand in for the research engine subprocess."""

    captured: dict = {}

    async def fake(url, output_dir, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        fake_engine_output(Path(output_dir), captured.get("kb"), captured.get("source", SOURCE))
        return Path(output_dir)

    monkeypatch.setattr(product_pipeline, "research_product", fake)
    return captured


# --- engine discovery ----------------------------------------------------------


def test_the_engine_bundled_with_the_project_is_found_without_configuration(
    monkeypatch,
) -> None:
    """One checkout is one product; no environment variable should be needed."""

    monkeypatch.delenv(product_pipeline.ENGINE_DIR_ENV, raising=False)
    assert product_pipeline.BUNDLED_ENGINE_DIR.name == "product_research"
    assert (product_pipeline.BUNDLED_ENGINE_DIR / "main.py").is_file()
    assert engine_dir() == product_pipeline.BUNDLED_ENGINE_DIR
    assert engine_available() is True


def test_engine_directory_is_configurable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(product_pipeline.ENGINE_DIR_ENV, str(tmp_path))
    assert engine_dir() == tmp_path
    assert engine_available() is False
    (tmp_path / "main.py").write_text("")
    assert engine_available() is True


@pytest.mark.asyncio
async def test_a_missing_engine_is_reported_not_crashed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(product_pipeline.ENGINE_DIR_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(ProductPipelineError, match="was not found"):
        await research_product("https://example.com", tmp_path / "out")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["not a url", "javascript:alert(1)", "ftp://x", ""])
async def test_a_bad_url_never_reaches_a_subprocess(monkeypatch, tmp_path, url: str) -> None:
    monkeypatch.setenv(product_pipeline.ENGINE_DIR_ENV, str(tmp_path))
    (tmp_path / "main.py").write_text("")
    with pytest.raises(ProductPipelineError, match="not a valid"):
        await research_product(url, tmp_path / "out")


# --- the gate ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verified_product_activates_and_becomes_selectable(
    stub_research, tmp_path
) -> None:
    engine = TaskEngine(user_contracts_dir=tmp_path / "tasks")
    report, activated = await build_task_from_url(
        "https://streamly.example",
        task_id="streamly_sales",
        agent_name="Adam",
        activate_when_clean=True,
        engine=engine,
    )
    assert report.can_auto_apply, report.blocking
    assert activated is True
    assert (tmp_path / "tasks" / "streamly_sales.yaml").is_file()
    assert engine.get_contract("streamly_sales") is not None


@pytest.mark.asyncio
async def test_an_unverifiable_price_is_never_activated(stub_research, tmp_path) -> None:
    bad = json.loads(json.dumps(KB))
    bad["commercials_pricing"]["plans"][0]["price_monthly"] = "$999"
    stub_research["kb"] = bad
    engine = TaskEngine(user_contracts_dir=tmp_path / "tasks")

    report, activated = await build_task_from_url(
        "https://streamly.example", task_id="streamly_sales",
        activate_when_clean=True, engine=engine,
    )

    assert activated is False
    assert not report.can_auto_apply
    assert not (tmp_path / "tasks" / "streamly_sales.yaml").exists()


@pytest.mark.asyncio
async def test_activation_is_opt_in(stub_research, tmp_path) -> None:
    engine = TaskEngine(user_contracts_dir=tmp_path / "tasks")
    report, activated = await build_task_from_url(
        "https://streamly.example", task_id="streamly_sales",
        activate_when_clean=False, engine=engine,
    )
    assert report.can_auto_apply, report.blocking
    assert activated is False
    assert not (tmp_path / "tasks" / "streamly_sales.yaml").exists()


@pytest.mark.asyncio
async def test_progress_reaches_the_studio(stub_research, tmp_path) -> None:
    lines: list[str] = []

    async def sink(message: str) -> None:
        lines.append(message)

    await build_task_from_url(
        "https://streamly.example", task_id="streamly_sales",
        engine=TaskEngine(user_contracts_dir=tmp_path / "tasks"), progress=sink,
    )
    assert any("Verifying" in line for line in lines)
    assert any("READY" in line or "BLOCKED" in line for line in lines)


@pytest.mark.asyncio
async def test_the_workspace_is_cleaned_up(stub_research, tmp_path, monkeypatch) -> None:
    seen: list[Path] = []
    original = product_pipeline.research_product

    async def spy(url, output_dir, **kwargs):
        seen.append(Path(output_dir))
        return await original(url, output_dir, **kwargs)

    monkeypatch.setattr(product_pipeline, "research_product", spy)
    await build_task_from_url(
        "https://streamly.example", task_id="streamly_sales",
        engine=TaskEngine(user_contracts_dir=tmp_path / "tasks"),
    )
    assert seen and not seen[0].exists()


def test_report_payload_carries_what_the_operator_must_see() -> None:
    from phone_agent_gateway.ai_bridge.tasks.product_import import FactCheck, ImportReport

    report = ImportReport(
        task_id="t", product_name="P",
        contract={"knowledge": {"a": "b"}, "allowed_tools": ["callback_schedule"]},
        accepted=(FactCheck("pricing", "x", True),),
        rejected=(FactCheck("security_tls", "y", False, "'TLS' not in source"),),
        blocking=("unverifiable security_tls",),
    )
    payload = report_payload(report, activated=False)
    assert payload["can_auto_apply"] is False
    assert payload["blocking"] == ["unverifiable security_tls"]
    assert payload["dropped"][0]["reason"] == "'TLS' not in source"
    assert payload["knowledge_count"] == 1


# --- model discovery -----------------------------------------------------------


def no_subscriptions(monkeypatch) -> None:
    """Ignore whichever local logins this developer machine happens to have."""

    import sys

    sys.path.insert(0, str(product_pipeline.engine_dir()))
    from src.extractor import subscription_providers

    monkeypatch.setattr(
        subscription_providers, "available_subscription_providers", dict
    )


def test_only_providers_with_a_usable_model_are_offered(monkeypatch) -> None:
    """Offering a provider with nothing behind it ends in a 404 after the crawl."""

    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    no_subscriptions(monkeypatch)

    class NoOllama:
        status_code = 503

    monkeypatch.setattr(product_pipeline.httpx, "get", lambda *a, **k: NoOllama())
    providers = product_pipeline.available_extraction_models()
    # Every provider is still listed; none is usable, and each says why.
    assert set(providers) == {name for name, _, _ in product_pipeline.KNOWN_PROVIDERS}
    assert not any(p["available"] for p in providers.values())
    assert providers["openai"]["reason"] == "set OPENAI_API_KEY"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert product_pipeline.available_extraction_models()["openai"]["available"] is True


def test_installed_ollama_models_are_listed_local_first(monkeypatch) -> None:
    """A cloud tag still needs an Ollama account, so local models come first."""

    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    no_subscriptions(monkeypatch)

    class Tags:
        status_code = 200

        @staticmethod
        def json():
            return {
                "models": [
                    {"name": "gemma4:31b-cloud"},
                    {"name": "qwen3.8:latest"},
                    {"name": "deepseek:cloud"},
                    {"name": "qwen3.5:4b-mlx"},
                ]
            }

    monkeypatch.setattr(product_pipeline.httpx, "get", lambda *a, **k: Tags())
    models = product_pipeline.available_extraction_models()["ollama"]["models"]
    # "-cloud" and ":cloud" are both cloud tags; neither may outrank a local model.
    assert models[:2] == ["qwen3.5:4b-mlx", "qwen3.8:latest"]
    assert models[2:] == ["deepseek:cloud", "gemma4:31b-cloud"]


def test_a_chosen_model_reaches_the_engine(stub_research, tmp_path) -> None:
    import asyncio

    asyncio.run(
        build_task_from_url(
            "https://streamly.example", task_id="streamly_sales",
            provider="ollama", model="qwen3.8:latest",
            engine=TaskEngine(user_contracts_dir=tmp_path / "tasks"),
        )
    )
    assert stub_research["model"] == "qwen3.8:latest"
    assert stub_research["provider"] == "ollama"


def test_subscription_logins_are_offered_alongside_ollama(monkeypatch) -> None:
    """A ChatGPT or Google login the operator already has needs no API key."""

    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    class NoOllama:
        status_code = 503

    monkeypatch.setattr(product_pipeline.httpx, "get", lambda *a, **k: NoOllama())

    import sys
    sys.path.insert(0, str(product_pipeline.engine_dir()))
    from src.extractor import subscription_providers

    monkeypatch.setattr(
        subscription_providers,
        "available_subscription_providers",
        lambda: {"codex": ["gpt-5.4-mini"], "antigravity": ["gemini-2.5-flash"]},
    )
    providers = product_pipeline.available_extraction_models()
    assert providers["codex"]["models"] == ["gpt-5.4-mini"]
    assert providers["codex"]["available"] is True
    assert providers["antigravity"]["models"] == ["gemini-2.5-flash"]


def test_a_missing_subscription_never_breaks_discovery(monkeypatch) -> None:
    """Discovery must degrade to whatever else is available, not raise."""

    import sys
    sys.path.insert(0, str(product_pipeline.engine_dir()))
    from src.extractor import subscription_providers

    def explode():
        raise RuntimeError("codex binary vanished")

    monkeypatch.setattr(
        subscription_providers, "available_subscription_providers", explode
    )
    product_pipeline.available_extraction_models()  # must not raise


def test_every_provider_is_listed_so_the_operator_can_choose(monkeypatch) -> None:
    """Hiding an unreachable provider makes a missing login look like a missing feature."""

    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    no_subscriptions(monkeypatch)

    class NoOllama:
        status_code = 503

    monkeypatch.setattr(product_pipeline.httpx, "get", lambda *a, **k: NoOllama())
    providers = product_pipeline.available_extraction_models()

    for name in ("codex", "antigravity", "ollama", "gemini", "openai", "anthropic"):
        assert name in providers, name
        assert providers[name]["label"]
        assert providers[name]["reason"], f"{name} must explain why it is unusable"
    # Known models are still offered so the choice is visible before signing in.
    assert providers["codex"]["models"]
    assert providers["antigravity"]["models"] == ["gemini-3.7-flash-tiered"]
