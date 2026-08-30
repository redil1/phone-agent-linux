"""
High-performance HTML to Clean Markdown / Text Parser.
Extracts semantic content while stripping UI boilerplate, cookies, navbars, and script tags.
"""

import re
from urllib.parse import urljoin, urlparse


class HTMLToMarkdownParser:
    """Converts HTML pages into clean, LLM-optimized Markdown."""

    def __init__(self):
        # Tags to remove entirely along with their content
        self.drop_tags = [
            r'<script[\s\S]*?</script>',
            r'<style[\s\S]*?</style>',
            r'<noscript[\s\S]*?</noscript>',
            r'<svg[\s\S]*?</svg>',
            r'<nav[\s\S]*?</nav>',
            r'<footer[\s\S]*?</footer>',
            r'<header[\s\S]*?</header>',
            r'<form[\s\S]*?</form>',
            r'<iframe[\s\S]*?</iframe>',
            r'<!--[\s\S]*?-->',
        ]

    def clean_html(self, html_content: str) -> str:
        """Removes unwanted tags and scripts from raw HTML."""
        text = html_content
        for pattern in self.drop_tags:
            text = re.sub(pattern, ' ', text, flags=re.IGNORECASE)
        return text

    def html_to_markdown(self, html_content: str, base_url: str = "") -> str:
        """Transforms HTML into clean markdown."""
        if not html_content:
            return ""

        clean = self.clean_html(html_content)

        # Convert headings (h1 - h6)
        clean = re.sub(r'<h1[^>]*>([\s\S]*?)</h1>', r'\n\n# \1\n\n', clean, flags=re.IGNORECASE)
        clean = re.sub(r'<h2[^>]*>([\s\S]*?)</h2>', r'\n\n## \1\n\n', clean, flags=re.IGNORECASE)
        clean = re.sub(r'<h3[^>]*>([\s\S]*?)</h3>', r'\n\n### \1\n\n', clean, flags=re.IGNORECASE)
        clean = re.sub(r'<h4[^>]*>([\s\S]*?)</h4>', r'\n\n#### \1\n\n', clean, flags=re.IGNORECASE)
        clean = re.sub(r'<h[56][^>]*>([\s\S]*?)</h[56]>', r'\n\n##### \1\n\n', clean, flags=re.IGNORECASE)

        # Convert strong / bold
        clean = re.sub(r'<(strong|b)[^>]*>([\s\S]*?)</\1>', r'**\2**', clean, flags=re.IGNORECASE)
        # Convert em / italic
        clean = re.sub(r'<(em|i)[^>]*>([\s\S]*?)</\1>', r'*\2*', clean, flags=re.IGNORECASE)

        # Convert list items
        clean = re.sub(r'<li[^>]*>([\s\S]*?)</li>', r'\n* \1', clean, flags=re.IGNORECASE)
        clean = re.sub(r'</?(ul|ol)[^>]*>', r'\n', clean, flags=re.IGNORECASE)

        # Convert paragraphs & line breaks
        clean = re.sub(r'<p[^>]*>([\s\S]*?)</p>', r'\n\n\1\n\n', clean, flags=re.IGNORECASE)
        clean = re.sub(r'<br\s*/?>', r'\n', clean, flags=re.IGNORECASE)
        clean = re.sub(r'<hr\s*/?>', r'\n---\n', clean, flags=re.IGNORECASE)

        # Convert tables simply
        clean = re.sub(r'<th[^>]*>([\s\S]*?)</th>', r'| **\1** ', clean, flags=re.IGNORECASE)
        clean = re.sub(r'<td[^>]*>([\s\S]*?)</td>', r'| \1 ', clean, flags=re.IGNORECASE)
        clean = re.sub(r'<tr[^>]*>', r'\n', clean, flags=re.IGNORECASE)
        clean = re.sub(r'</tr>', r'|', clean, flags=re.IGNORECASE)
        clean = re.sub(r'</?(table|tbody|thead|tfoot)[^>]*>', r'\n', clean, flags=re.IGNORECASE)

        # Strip remaining HTML tags
        clean = re.sub(r'<[^>]+>', ' ', clean)

        # Fix HTML entities
        clean = clean.replace('&nbsp;', ' ')
        clean = clean.replace('&amp;', '&')
        clean = clean.replace('&lt;', '<')
        clean = clean.replace('&gt;', '>')
        clean = clean.replace('&quot;', '"')
        clean = clean.replace('&#39;', "'")
        clean = clean.replace('&mdash;', '—')
        clean = clean.replace('&ndash;', '–')

        # Clean whitespace and repeated blank lines
        lines = [line.strip() for line in clean.split('\n')]
        formatted_lines = []
        consecutive_blank = 0
        for line in lines:
            if not line:
                consecutive_blank += 1
                if consecutive_blank <= 2:
                    formatted_lines.append("")
            else:
                consecutive_blank = 0
                formatted_lines.append(line)

        return '\n'.join(formatted_lines).strip()

    def extract_links(self, html_content: str, base_url: str) -> list[str]:
        """Extracts internal links matching the domain."""
        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc.lower()

        links = []
        raw_links = re.findall(r'href=[\'"]?([^\'" >]+)', html_content, flags=re.IGNORECASE)
        for raw in raw_links:
            # Skip anchors, tel, mailto, javascript, image files
            if raw.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                continue
            if any(raw.lower().endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.pdf', '.zip', '.mp4')):
                continue

            full_url = urljoin(base_url, raw)
            parsed_full = urlparse(full_url)

            # Strip fragments and query parameters
            clean_url = f"{parsed_full.scheme}://{parsed_full.netloc}{parsed_full.path}".rstrip('/')
            
            # Keep same domain only
            if parsed_full.netloc.lower() == base_domain and clean_url not in links:
                links.append(clean_url)

        return links
