"""
Asynchronous Multi-Page Product Web Crawler.
Discovers sitemaps, navigates priority product pages, and returns structured Markdown per category.
"""

import asyncio
import re
from urllib.parse import urljoin, urlparse

import httpx

from .html_parser import HTMLToMarkdownParser

PRIORITY_KEYWORDS = {
    "pricing": ["pricing", "plans", "cost", "tier", "subscription", "buy"],
    "features": ["features", "product", "platform", "capabilities", "modules", "overview", "solutions"],
    "integrations": ["integrations", "apps", "connectors", "plugins", "marketplace", "api"],
    "security": ["security", "compliance", "privacy", "trust", "gdpr", "hipaa", "soc2", "terms", "sla"],
    "case_studies": ["customers", "case-studies", "testimonials", "stories", "results"],
    "docs_faq": ["faq", "docs", "documentation", "support", "help", "guide", "knowledge-base"],
    "about": ["about", "company", "mission", "contact"]
}


class CrawledPage:
    def __init__(self, url: str, category: str, title: str, markdown: str, raw_html: str = ""):
        self.url = url
        self.category = category
        self.title = title
        self.markdown = markdown
        self.raw_html = raw_html

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "category": self.category,
            "title": self.title,
            "markdown": self.markdown
        }


class WebCrawler:
    def __init__(
        self,
        max_pages: int = 25,
        concurrency: int = 5,
        timeout: float = 15.0,
        user_agent: str | None = None
    ):
        self.max_pages = max_pages
        self.concurrency = concurrency
        self.timeout = timeout
        self.parser = HTMLToMarkdownParser()
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )

    def categorize_url(self, url: str) -> str:
        """Determines the semantic category of a URL based on path keywords."""
        path = urlparse(url).path.lower()
        if not path or path == "/":
            return "homepage"

        for category, keywords in PRIORITY_KEYWORDS.items():
            for kw in keywords:
                if kw in path:
                    return category
        return "general"

    async def fetch_page(self, client: httpx.AsyncClient, url: str) -> CrawledPage | None:
        """Fetches a single page and converts to markdown."""
        try:
            headers = {"User-Agent": self.user_agent}
            response = await client.get(url, headers=headers, follow_redirects=True, timeout=self.timeout)
            
            if response.status_code != 200:
                return None
            
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                return None

            html_text = response.text
            
            # Extract page title
            title_match = re.search(r'<title[^>]*>(.*?)</title>', html_text, flags=re.IGNORECASE)
            title = title_match.group(1).strip() if title_match else url

            markdown = self.parser.html_to_markdown(html_text, base_url=url)
            category = self.categorize_url(str(response.url))

            return CrawledPage(
                url=str(response.url),
                category=category,
                title=title,
                markdown=markdown,
                raw_html=html_text
            )
        except Exception:
            return None

    async def discover_sitemap_urls(self, client: httpx.AsyncClient, base_url: str) -> list[str]:
        """Tries to find URLs from standard sitemap locations."""
        discovered = []
        sitemap_locations = [
            urljoin(base_url, "/sitemap.xml"),
            urljoin(base_url, "/sitemap_index.xml"),
            urljoin(base_url, "/robots.txt")
        ]

        headers = {"User-Agent": self.user_agent}
        for s_url in sitemap_locations:
            try:
                res = await client.get(s_url, headers=headers, follow_redirects=True, timeout=5.0)
                if res.status_code == 200:
                    # Find all URLs in sitemap xml or robots.txt
                    found = re.findall(r'<loc>(https?://[^<]+)</loc>', res.text)
                    if not found:
                        found = re.findall(r'Sitemap:\s*(https?://[^\s]+)', res.text, flags=re.IGNORECASE)
                    for u in found:
                        if u not in discovered:
                            discovered.append(u)
            except Exception:
                continue

        return discovered

    async def crawl(self, root_url: str) -> list[CrawledPage]:
        """Crawl the website starting from root_url up to max_pages."""
        # Ensure proper URL format
        if not root_url.startswith(("http://", "https://")):
            root_url = "https://" + root_url
        
        parsed_root = urlparse(root_url)
        clean_root = f"{parsed_root.scheme}://{parsed_root.netloc}"

        visited_urls: set[str] = set()
        # Seed standard priority URLs directly
        priority_seeds = [
            root_url,
            urljoin(clean_root, "/pricing"),
            urljoin(clean_root, "/plans"),
            urljoin(clean_root, "/features"),
            urljoin(clean_root, "/security"),
        ]
        queue: list[str] = []
        for seed in priority_seeds:
            if seed not in queue:
                queue.append(seed)

        crawled_pages: list[CrawledPage] = []
        semaphore = asyncio.Semaphore(self.concurrency)

        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        # TLS verification stays on. These pages become facts the agent states to
        # customers as verified truth, so accepting any certificate would let a
        # network attacker dictate the prices it quotes.
        async with httpx.AsyncClient(limits=limits, verify=True) as client:
            # 1. Check sitemaps for priority URLs
            sitemap_urls = await self.discover_sitemap_urls(client, clean_root)
            if sitemap_urls:
                # Prioritize pages matching priority keywords
                prioritized = []
                regular = []
                for u in sitemap_urls:
                    if any(kw in u.lower() for keywords in PRIORITY_KEYWORDS.values() for kw in keywords):
                        prioritized.append(u)
                    else:
                        regular.append(u)
                for u in (prioritized[:15] + regular[:10]):
                    if u not in queue:
                        queue.append(u)


            # 2. Main crawl loop
            while queue and len(crawled_pages) < self.max_pages:
                current_batch = []
                while queue and len(current_batch) < self.concurrency:
                    url = queue.pop(0)
                    if url not in visited_urls:
                        visited_urls.add(url)
                        current_batch.append(url)

                if not current_batch:
                    break

                async def fetch_task(u: str):
                    async with semaphore:
                        return await self.fetch_page(client, u)

                results = await asyncio.gather(*(fetch_task(u) for u in current_batch))

                for page in results:
                    if page and page.markdown.strip():
                        crawled_pages.append(page)
                        
                        # Extract links from page if we need more
                        if len(crawled_pages) + len(queue) < self.max_pages:
                            links = self.parser.extract_links(page.raw_html, page.url)
                            # Sort by priority keywords
                            for link in links:
                                if link not in visited_urls and link not in queue:
                                    if any(kw in link.lower() for kws in PRIORITY_KEYWORDS.values() for kw in kws):
                                        queue.insert(0, link)  # Push high priority to front
                                    else:
                                        queue.append(link)

        return crawled_pages

    def aggregate_markdown(self, pages: list[CrawledPage]) -> str:
        """Combines crawled pages into a single categorized document for LLM ingestion."""
        by_category: dict[str, list[CrawledPage]] = {}
        for p in pages:
            by_category.setdefault(p.category, []).append(p)

        sections = []
        for cat, plist in by_category.items():
            sections.append("\n\n==========================================")
            sections.append(f"SECTION CATEGORY: {cat.upper()}")
            sections.append("==========================================\n")
            for p in plist:
                sections.append(f"\n--- SOURCE URL: {p.url} (Title: {p.title}) ---\n")
                sections.append(p.markdown)

        return "\n".join(sections)
