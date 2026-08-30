"""Gather market context for positioning. Never for quoting.

A salesperson who knows the category sells better than one who only knows the
brochure. This fetches a small amount of category context — what buyers compare,
what they worry about, the words the market uses — so the agent can choose its
angle and anticipate concerns.

What comes back is *not* fact material. It never enters the knowledge block, it
is never verified, and the extraction prompt forbids quoting it. Repeating a
competitor's marketing, or a price that changed last week, to a live customer is
a claim the company cannot stand behind.

Kept deliberately small and polite: a handful of pages, robots.txt respected,
TLS verified, and any failure is survivable because the product's own site is
what the agent actually sells from.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .html_parser import HTMLToMarkdownParser

logger = logging.getLogger("MarketCrawler")

MAX_MARKET_PAGES = 6
PAGE_TIMEOUT_SECS = 8.0
TOTAL_TIMEOUT_SECS = 45.0
MAX_PAGE_CHARS = 6_000
USER_AGENT = "PhoneAgentResearch/1.0 (+market context; respects robots.txt)"

# A search service that answers unattended clients, unlike the scraped engines
# which return a bot challenge. Override for a different deployment; when it is
# unreachable the extraction still runs on the model's own category knowledge.
SEARCH_API_URL = os.getenv(
    "PHONE_AGENT_SEARCH_API", "http://127.0.0.1:8000/api/search"
)
SEARCH_TIMEOUT_SECS = 30.0

# Roughly a third of results are Google redirect wrappers rather than the page
# itself, and are not fetchable.
_REDIRECT_MARKERS = ("google.com/goto", "/url?", "google.com/search")

# Domains that return navigation and login walls rather than readable opinion.
# Forums and discussion sites are deliberately kept: complaint threads are the
# most honest account of what this market actually worries about.
_LOW_SIGNAL_DOMAINS = frozenset(
    {
        "facebook.com", "instagram.com", "tiktok.com", "pinterest.com",
        "linkedin.com", "youtube.com", "x.com", "twitter.com",
    }
)


def market_queries(product_name: str, category_hint: str = "") -> list[str]:
    """What a salesperson would look up before calling into a market."""

    subject = (category_hint or product_name).strip()
    return [
        f"{subject} alternatives comparison",
        f"{subject} common problems complaints",
        f"why customers switch {subject}",
    ]


def _is_usable(url: str) -> bool:
    if not url.startswith("http"):
        return False
    if any(marker in url for marker in _REDIRECT_MARKERS):
        return False
    domain = urlparse(url).netloc.replace("www.", "").lower()
    return domain not in _LOW_SIGNAL_DOMAINS


async def _search(client: httpx.AsyncClient, queries: list[str], per_query: int) -> list[str]:
    """Ask the search service for all queries at once, ranked order preserved."""

    try:
        response = await client.post(
            SEARCH_API_URL,
            json={"queries": queries, "num_results": per_query, "hl": "en", "gl": "us"},
            timeout=SEARCH_TIMEOUT_SECS,
        )
        if response.status_code != 200:
            logger.info("Market search returned HTTP %s", response.status_code)
            return []
        payload = response.json()
    except Exception as exc:
        logger.info("Market search unavailable: %s", exc)
        return []

    ranked = sorted(
        (r for r in payload.get("results", []) if isinstance(r, dict)),
        key=lambda r: r.get("rank", 99),
    )
    urls = [str(r.get("url", "")) for r in ranked]
    usable = [url for url in urls if _is_usable(url)]
    logger.info(
        "Market search: %d result(s), %d usable after filtering redirects and walled sites",
        len(urls),
        len(usable),
    )
    return usable


async def _allowed_by_robots(client: httpx.AsyncClient, url: str) -> bool:
    """Honour robots.txt using its real semantics.

    Matching "Disallow:" lines with a regex ignored the user-agent sections they
    belong to, so a site that blocks one named crawler and welcomes everyone
    else read as fully closed. That silently discarded good sources such as
    cnet.com. RobotFileParser applies the rules to our agent only.
    """

    parsed = urlparse(url)
    try:
        response = await client.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        if response.status_code != 200:
            return True
        rules = RobotFileParser()
        rules.parse(response.text.splitlines())
        return rules.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def _is_on_topic(text: str, terms: list[str]) -> bool:
    """Keep pages that are actually about this category.

    A commercial search term attracts pages that merely mention it; one run
    surfaced a diabetes retailer. A page that never repeats the subject is
    noise, and noise in the positioning is worse than no market context.
    """

    lowered = text.lower()
    return any(lowered.count(term) >= 3 for term in terms)


async def gather_market_context(
    product_name: str,
    *,
    own_domain: str = "",
    category_hint: str = "",
    max_pages: int = MAX_MARKET_PAGES,
) -> str:
    """Return a short block of category context, or an empty string.

    Failure is always acceptable: the product's own site is what the agent
    sells from, and market context only shapes the angle.
    """

    parser = HTMLToMarkdownParser()
    topic_terms = [
        term.lower()
        for term in re.findall(r"[\w-]+", f"{product_name} {category_hint}")
        if len(term) > 3
    ][:6]
    collected: list[str] = []
    seen_domains: set[str] = set()
    if own_domain:
        seen_domains.add(own_domain.replace("www.", ""))

    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECS):
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=PAGE_TIMEOUT_SECS,
                verify=True,
            ) as client:
                candidates = await _search(
                    client, market_queries(product_name, category_hint), per_query=8
                )

                for url in candidates:
                    if len(collected) >= max_pages:
                        break
                    domain = urlparse(url).netloc.replace("www.", "")
                    # One page per domain, and never the company's own site:
                    # that is gathered separately and held to a higher standard.
                    if not domain or domain in seen_domains:
                        continue
                    if not await _allowed_by_robots(client, url):
                        logger.info("Skipping %s: robots.txt disallows it", domain)
                        continue
                    try:
                        response = await client.get(url)
                        if response.status_code != 200:
                            continue
                        text = parser.html_to_markdown(response.text, url)
                    except Exception:
                        continue
                    if len(text.strip()) < 400 or not _is_on_topic(text, topic_terms):
                        logger.info("Skipping %s: not about this category", domain)
                        continue
                    seen_domains.add(domain)
                    collected.append(f"## Market source: {domain}\n{text[:MAX_PAGE_CHARS]}")
    except TimeoutError:
        logger.info("Market context gathering timed out; continuing with what was found")
    except Exception as exc:
        logger.info("Market context unavailable: %s", exc)

    logger.info("Market context: %d source(s) from %d domain(s)", len(collected), len(seen_domains))
    return "\n\n".join(collected)
