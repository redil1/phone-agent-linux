# Live Web Research

PhoneAgent exposes one bounded `web_research` function to the OpenAI Realtime agent. It is
available during both Realtime WebSocket and WebRTC calls and does not modify GSM, Android media,
WhatsApp voice, or OpenWA messaging.

## Runtime flow

1. The caller asks for current or missing public information.
2. The agent says one short waiting sentence in the caller's language.
3. Bing and the optional independent DuckDuckGo search run concurrently. Results are merged and
   deduplicated without a deterministic relevance decision.
4. Technical policy removes only malformed, duplicate, administratively blocked, or unsafe URLs.
5. PhoneAgent reads selected pages concurrently with bounded `aiohttp` requests and extracts
   the main article with Trafilatura.
6. Only pages that fail normal extraction are offered to the isolated Crawl4AI browser fallback.
7. The tool returns bounded search cards and page evidence: titles, URLs, snippets, provider,
   provider position, extraction method, dates when available, latency and warnings. It explicitly
   does not judge relevance, freshness, credibility, truth, confidence, or the next action. The
   Realtime AI performs that evaluation for the current conversation and task.

If the first evidence is insufficient or contradictory, the AI may perform another materially
different search aimed at the specific missing fact. It stops immediately when evidence is enough,
never repeats the same query, and may use at most three searches for one information need. After
the third attempt it must explain the remaining uncertainty rather than continue searching. This
policy is generic: it applies regardless of call channel, task, subject, or possible next action.

The normal path has no browser startup cost. A successful live test on this Mac read the official
Crawl4AI documentation in about 1.9 seconds and the official OpenAI Realtime documentation in
about 1.7 seconds. Internet latency and remote-site behavior vary, so these are observations rather
than guarantees.

## Studio controls

Open **Tools & MCP → Live Web Research**.

- **Activate live web research:** Adds `web_research` to eligible live Realtime calls. There is no
  per-use human approval.
- **Respect robots.txt:** Refuses normal page reading when the publisher disallows it.
- **Include independent DuckDuckGo results:** Gives the AI evidence from a second search provider
  and keeps discovery available if Bing is challenged or unavailable.
- **Task IDs:** Blank means every task. Otherwise enter one task ID per line.
- **Bing result candidates:** Number of search cards considered, from 3 to 20.
- **Best pages to read:** Number of top-ranked pages read, from 1 to 5. Three is the recommended
  balance between comparison quality and call latency.
- **Parallel page readers:** Maximum simultaneous static page downloads, from 1 to 5.
- **Safe search:** `Moderate` is the useful default for broad business research. `Strict` may hide
  legitimate results in sensitive categories.
- **Search language:** Automatic follows the query; English and French can be forced.
- **Country code:** Two-letter search region. `US` provides the broadest English index by default;
  change it when local results matter.
- **Total deadline:** Hard end-to-end time budget. The call receives a clear failure instead of an
  endless search.
- **Bing/page/fallback timeouts:** Individual network budgets inside the total deadline.
- **Characters per source / total evidence:** Bounds how much untrusted text reaches Realtime.
- **Cache lifetime / entries:** Reuses recent identical research to reduce latency and load.
- **Preferred domains:** Adds a ranking bonus to reviewed domains; it is not an allowlist.
- **Blocked domains:** Rejects a domain and all its subdomains.
- **Crawl4AI controls:** Enable the JavaScript fallback, set its localhost URL, private token,
  timeout and maximum fallback pages.

**Run Real Search Test** uses the unsaved draft settings and displays total latency, all search
cards returned to the AI, extracted page titles, URLs, provider, method, character count and safe
previews. **Save & Hot Reload** stores the exact settings and updates an active Realtime call within
about one second.

## Crawl4AI sidecar

Install or upgrade the pinned sidecar:

```bash
tools/install_crawl4ai_sidecar.sh
```

The installer creates a private API token, stores it in the private web-research configuration,
starts the exact `unclecode/crawl4ai:0.9.2` image digest, waits for health and requires a real
authenticated browser crawl to succeed. The service binds only to `127.0.0.1:11235`, runs with a
read-only root filesystem, no added Linux capabilities, no privilege escalation, bounded CPU,
memory, PIDs, queue size, crawl depth, pages and wall-clock time. Hooks, webhooks and insecure TLS
are disabled.

Stop it without deleting configuration or data:

```bash
tools/stop_crawl4ai_sidecar.sh
```

## Security and truthfulness

- Search result URLs and every redirect are limited to HTTP(S), ports 80/443 and public IP space.
  Credentials, loopback, private, link-local, multicast, reserved and unspecified destinations are
  rejected to reduce SSRF risk.
- Response size, redirects, concurrency, source text and total execution time are bounded.
- Search and page text are explicitly labeled untrusted evidence. The persona tells the model to
  ignore instructions in pages, compare sources, cite or offer URLs, and state uncertainty.
- Search results are not deterministically rejected for semantic irrelevance. The AI receives the
  bounded evidence and must evaluate whether it answers the caller's request.
- Audit records store state, latency and source counts, not the query or page content.
  Studio connection-test queries are represented only by a short hash.
- The private Crawl4AI token is masked in Studio and stored mode `0600`.

## Operational limits

Bing and DuckDuckGo HTML are unofficial, no-cost discovery interfaces. Either provider may rate
limit, challenge or change markup without notice. The dual-provider parser, technical safety
boundary, cache and honest failure behavior reduce this risk but cannot turn a free service into an
availability-guaranteed commercial API. Crawl4AI improves JavaScript page reading; it does not
solve a search provider outage.

Configuration: `~/.config/phone-agent/web-research.json`

Sidecar files: `~/.local/share/phone-agent/crawl4ai/`

Private token: `~/.config/phone-agent/crawl4ai-api-token`
