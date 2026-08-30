from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from phone_agent_gateway.ai_bridge.runtime_config import ProviderConfig
from phone_agent_gateway.ai_bridge.web_research import (
    WEB_RESEARCH_TOOL_NAME,
    WebResearchConfig,
    WebResearchConfigStore,
    WebResearchEngine,
    WebResearchError,
    WebResearchToolRuntime,
    _read_bounded,
)
from phone_agent_gateway.ai_bridge.web_server import PhoneAgentWebServer


class _Content:
    def __init__(self, body: bytes, *, chunk_size: int | None = None) -> None:
        self.body = body
        self.offset = 0
        self.chunk_size = chunk_size

    async def read(self, limit: int) -> bytes:
        if self.chunk_size is not None:
            limit = min(limit, self.chunk_size)
        chunk = self.body[self.offset : self.offset + limit]
        self.offset += len(chunk)
        return chunk


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        url: str = "https://example.com/article",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.url = url
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.charset = "utf-8"
        self.content = _Content(body)

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _Session:
    def __init__(self, *, get: _Response | None = None, post: _Response | None = None) -> None:
        self.get_response = get
        self.post_response = post
        self.last_get: tuple[str, dict[str, Any]] | None = None
        self.last_post: tuple[str, dict[str, Any]] | None = None

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.last_get = (url, kwargs)
        assert self.get_response is not None
        return self.get_response

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.last_post = (url, kwargs)
        assert self.post_response is not None
        return self.post_response


def _candidate(url: str = "https://example.com/article") -> dict[str, Any]:
    return {
        "title": "OpenAI Realtime API updates",
        "url": url,
        "snippet": "Current OpenAI Realtime API updates and documentation.",
        "score": 5.2,
        "provider": "bing",
        "provider_position": 1,
    }


@pytest.mark.asyncio
async def test_bounded_reader_accumulates_partial_network_chunks() -> None:
    payload = b'{"results":[{"success":true}]}'
    assert await _read_bounded(_Content(payload, chunk_size=3), 1_000) == payload
    assert len(await _read_bounded(_Content(b"x" * 20, chunk_size=3), 8)) == 9


def test_config_store_masks_secret_and_preserves_private_permissions(tmp_path: Path) -> None:
    store = WebResearchConfigStore(tmp_path / "web-research.json")
    payload = WebResearchConfig(crawl4ai_token="very-secret").model_dump(mode="json")
    saved = store.save(payload)

    assert saved.revision == 1
    assert store.public_state()["crawl4ai_token"] == "••••••••"
    assert "very-secret" in store.path.read_text(encoding="utf-8")
    assert os.stat(store.path).st_mode & 0o777 == 0o600

    public = store.public_state()
    public["pages_to_read"] = 2
    updated = store.save(public)
    assert updated.crawl4ai_token == "very-secret"
    assert updated.revision == 2


def test_config_validation_rejects_unsafe_or_inconsistent_settings() -> None:
    with pytest.raises(ValueError, match="remote Crawl4AI requires HTTPS"):
        WebResearchConfig(crawl4ai_url="http://example.com:11235")
    with pytest.raises(ValueError, match="cannot exceed"):
        WebResearchConfig(search_results=3, pages_to_read=4)
    with pytest.raises(ValueError, match="invalid hostname"):
        WebResearchConfig(blocked_domains=["bad..example"])


def test_crawl4ai_sidecar_is_pinned_local_and_resource_bounded() -> None:
    project = Path(__file__).resolve().parents[2]
    compose = (project / "integrations/crawl4ai/compose.yaml").read_text(encoding="utf-8")
    config = (project / "integrations/crawl4ai/config.yml").read_text(encoding="utf-8")

    assert "unclecode/crawl4ai:0.9.2@sha256:" in compose
    assert '127.0.0.1:${CRAWL4AI_PORT:-11235}:11235' in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose and "pids_limit: 512" in compose
    assert 'storage_uri: "redis://localhost:6379/0"' in config
    assert "max_pages: 5" in config and "wall_clock_s: 12" in config
    assert "enabled: false" in config  # webhooks remain disabled


@pytest.mark.asyncio
async def test_bing_discovery_filters_only_technical_policy_not_relevance() -> None:
    html = b"""
    <html><body>
      <li class="b_algo"><h2><a href="https://docs.openai.com/realtime">
      OpenAI Realtime API updates</a></h2><div class="b_caption"><p>
      Latest Realtime API documentation and changes.</p></div></li>
      <li class="b_algo"><h2><a href="https://docs.openai.com/realtime">
      OpenAI Realtime API updates duplicate</a></h2><p>
      Latest Realtime API documentation.</p></li>
      <li class="b_algo"><h2><a href="https://blocked.example/realtime">
      OpenAI Realtime API updates</a></h2><p>Latest OpenAI changes.</p></li>
      <li class="b_algo"><h2><a href="https://irrelevant.example/">
      Cooking guide</a></h2><p>How to make soup.</p></li>
    </body></html>
    """
    session = _Session(get=_Response(html))
    engine = WebResearchEngine(
        WebResearchConfig(blocked_domains=["blocked.example"]), session  # type: ignore[arg-type]
    )

    results = await engine._bing_search(
        "OpenAI latest official announcement within last 7 days and its publication date "
        "site:openai.com or on OpenAI blog",
        "en",
    )

    assert [item["url"] for item in results] == [
        "https://docs.openai.com/realtime",
        "https://irrelevant.example/",
    ]
    assert all(item["provider"] == "bing" for item in results)
    assert session.last_get is not None
    assert session.last_get[1]["params"]["count"] == 10
    assert session.last_get[1]["params"]["adlt"] == "moderate"


@pytest.mark.asyncio
async def test_discovery_returns_provider_diversity_for_agent_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = WebResearchEngine(WebResearchConfig(search_results=3), _Session())  # type: ignore[arg-type]
    bing = [
        {**_candidate("https://bing-one.example/"), "provider_position": 1},
        {**_candidate("https://bing-two.example/"), "provider_position": 2},
    ]
    duck = [
        {
            **_candidate("https://duck-one.example/"),
            "provider": "duckduckgo",
            "provider_position": 1,
        }
    ]

    async def bing_search(_query: str, _language: str) -> list[dict[str, Any]]:
        return bing

    async def duck_search(_query: str, _language: str) -> list[dict[str, Any]]:
        return duck

    monkeypatch.setattr(engine, "_bing_search", bing_search)
    monkeypatch.setattr(engine, "_duckduckgo_search", duck_search)

    results, warnings = await engine._discover("a long natural request", "en")

    assert [item["url"] for item in results] == [
        "https://bing-one.example/",
        "https://duck-one.example/",
        "https://bing-two.example/",
    ]
    assert warnings == []


@pytest.mark.asyncio
async def test_bing_challenge_fails_honestly() -> None:
    engine = WebResearchEngine(
        WebResearchConfig(),
        _Session(get=_Response(b"Verify you are human CAPTCHA")),  # type: ignore[arg-type]
    )
    with pytest.raises(WebResearchError, match="anti-bot challenge"):
        await engine._bing_search("OpenAI Realtime API updates", "en")


@pytest.mark.asyncio
async def test_duckduckgo_backup_decodes_and_ranks_direct_results() -> None:
    html = b"""
    <html><body><div class="result results_links results_links_deep web-result">
      <h2 class="result__title"><a class="result__a"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.crawl4ai.com%2F">
      Crawl4AI documentation</a></h2>
      <a class="result__snippet">Official Crawl4AI GitHub documentation and examples.</a>
    </div></body></html>
    """
    engine = WebResearchEngine(
        WebResearchConfig(), _Session(get=_Response(html))  # type: ignore[arg-type]
    )

    results = await engine._duckduckgo_search("Crawl4AI GitHub documentation", "en")

    assert results[0]["url"] == "https://docs.crawl4ai.com/"
    assert results[0]["title"] == "Crawl4AI documentation"


@pytest.mark.asyncio
async def test_ssrf_guard_rejects_private_and_invalid_ports() -> None:
    engine = WebResearchEngine(WebResearchConfig(), _Session())  # type: ignore[arg-type]
    with pytest.raises(WebResearchError, match="forbidden network"):
        await engine._validate_public_url("http://127.0.0.1/private")
    with pytest.raises(WebResearchError, match="invalid port"):
        await engine._validate_public_url("https://example.com:bad/page")


def test_trafilatura_static_extraction_returns_bounded_evidence() -> None:
    paragraph = "PhoneAgent live research evidence is current and useful. " * 20
    html = (
        "<html><head><title>Research result</title></head><body><article><p>"
        f"{paragraph}</p></article></body></html>"
    )
    engine = WebResearchEngine(
        WebResearchConfig(max_chars_per_source=500), _Session()  # type: ignore[arg-type]
    )

    source = engine._extract_static(html, "https://example.com/article", _candidate())

    assert source is not None
    assert source["extractor"] == "trafilatura"
    assert len(source["content"]) <= 500


@pytest.mark.asyncio
async def test_research_uses_cache_and_emits_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    engine = WebResearchEngine(
        WebResearchConfig(crawl4ai_enabled=False, duckduckgo_fallback_enabled=False),
        _Session(),  # type: ignore[arg-type]
        event_sink=events.append,
    )

    async def search(_query: str, _language: str) -> list[dict[str, Any]]:
        return [_candidate()]

    async def read(_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **_candidate(),
                "published_date": "2026-08-29",
                "extractor": "trafilatura",
                "content": "Verified current evidence. " * 30,
            }
        ]

    monkeypatch.setattr(engine, "_bing_search", search)
    monkeypatch.setattr(engine, "_read_static_sources", read)
    first = await engine.research("OpenAI Realtime API updates")
    second = await engine.research("OpenAI Realtime API updates")

    assert first["cache_hit"] is False
    assert first["evaluation_required"] is True
    assert first["confidence"] == "not_assessed_by_tool"
    assert first["search_results"][0]["provider"] == "bing"
    assert first["iteration_policy"]["max_searches_per_information_need"] == 3
    assert first["iteration_policy"]["never_repeat_the_same_query"] is True
    assert second["cache_hit"] is True
    assert any(event["state"] == "complete" for event in events)
    assert any(event["state"] == "cache_hit" for event in events)


@pytest.mark.asyncio
async def test_crawl4ai_fallback_contract_and_response_parsing() -> None:
    body = json.dumps(
        {
            "results": [
                {
                    "success": True,
                    "url": "https://example.com/article",
                    "markdown": {"raw_markdown": "Rendered JavaScript evidence. " * 30},
                    "metadata": {"title": "Rendered result", "date": "2026-08-29"},
                }
            ]
        }
    ).encode()
    session = _Session(post=_Response(body, headers={"Content-Type": "application/json"}))
    config = WebResearchConfig(crawl4ai_token="token", respect_robots_txt=True)
    engine = WebResearchEngine(config, session)  # type: ignore[arg-type]

    sources, warning = await engine._crawl4ai([_candidate()])

    assert warning is None
    assert sources[0]["extractor"] == "crawl4ai"
    assert session.last_post is not None
    assert session.last_post[1]["headers"]["Authorization"] == "Bearer token"
    request = session.last_post[1]["json"]
    assert request["urls"] == ["https://example.com/article"]
    assert request["browser_config"]["type"] == "BrowserConfig"
    assert request["crawler_config"]["params"]["check_robots_txt"] is True


@pytest.mark.asyncio
async def test_runtime_is_autopilot_task_scoped_and_closes() -> None:
    disabled = WebResearchToolRuntime(
        WebResearchConfig(task_ids=["another_task"]), task_id="current_task"
    )
    assert await disabled.start() == {}

    runtime = WebResearchToolRuntime(
        WebResearchConfig(task_ids=["current_task"]), task_id="current_task"
    )
    tools = await runtime.start()
    assert set(tools) == {WEB_RESEARCH_TOOL_NAME}
    definition = tools[WEB_RESEARCH_TOOL_NAME].definition
    assert "few seconds" in definition["description"]
    assert "without deciding relevance" in definition["description"]
    assert "at most three searches" in definition["description"]
    assert definition["parameters"]["required"] == ["query"]
    await runtime.close()
    assert runtime.session is None


@pytest.mark.asyncio
async def test_studio_can_save_and_visibly_test_web_research(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WebResearchConfigStore(tmp_path / "web-research.json")

    async def health(_self: WebResearchEngine) -> dict[str, Any]:
        return {"enabled": True, "reachable": True, "status_code": 200}

    async def research(
        _self: WebResearchEngine, query: str, language: str = "auto"
    ) -> dict[str, Any]:
        return {
            "completed": True,
            "query": query,
            "evaluation_required": True,
            "confidence": "not_assessed_by_tool",
            "search_results": [
                {
                    "title": "Search card",
                    "url": "https://example.com/current",
                    "snippet": "Search engine summary.",
                    "provider": "bing",
                    "provider_position": 1,
                }
            ],
            "sources": [
                {
                    "title": "Current official result",
                    "url": "https://example.com/current",
                    "published_date": "2026-08-29",
                    "extractor": "trafilatura",
                    "content": "Current source content " * 80,
                    "discovery_provider": "bing",
                    "provider_position": 1,
                }
            ],
            "searched_results": 10,
            "read_sources": 1,
            "elapsed_ms": 125.0,
            "cache_hit": False,
            "warnings": [],
            "security_notice": "untrusted evidence",
        }

    monkeypatch.setattr(WebResearchEngine, "crawl4ai_health", health)
    monkeypatch.setattr(WebResearchEngine, "research", research)
    server = PhoneAgentWebServer(
        config=ProviderConfig(),
        settings_path=tmp_path / "studio.json",
        web_research_config_store=store,
    )
    async with TestClient(TestServer(server.app)) as client:
        state = await (await client.get("/api/web-research")).json()
        assert state["connectivity"]["crawl4ai"]["reachable"] is True
        config = state["config"]
        config["search_results"] = 12
        saved = await (await client.post("/api/web-research", json={"config": config})).json()
        assert saved["config"]["search_results"] == 12
        tested = await (
            await client.post(
                "/api/web-research/test",
                json={"config": saved["config"], "query": "current service status"},
            )
        ).json()

    assert tested["status"] == "ok"
    assert tested["result"]["sources"][0]["content_chars"] > 1_000
    assert tested["result"]["evaluation_required"] is True
    assert tested["result"]["search_results"][0]["provider"] == "bing"
    assert len(tested["result"]["sources"][0]["content_preview"]) == 1_000
    assert store.load().search_results == 12


def test_studio_page_exposes_all_web_research_controls() -> None:
    page = (
        Path(__file__).resolve().parents[2] / "ai_bridge/web_static/index.html"
    ).read_text(encoding="utf-8")

    for element_id in (
        "web-research-enabled",
        "web-research-duckduckgo",
        "web-research-results",
        "web-research-pages",
        "web-research-concurrency",
        "web-research-overall-timeout",
        "web-research-source-chars",
        "web-research-cache-ttl",
        "web-research-crawl-enabled",
        "web-research-test-results",
        "web-research-toggle",
        "web-research-parameters",
        "openwa-toggle",
        "openwa-parameters",
    ):
        assert f'id="{element_id}"' in page
    assert "Run Real Search Test" in page
    assert "Save &amp; Hot Reload" in page
    assert "toggleToolParameters" in page
    assert "AI evaluation required" in page
    assert page.count("Collapse parameters") >= 2
