"""Enhanced web connector — crawls URLs with blocking detection, sitemap
discovery, recursive depth crawling, and intelligent HTML cleanup.

Ported from EnhancedWebScraper (Connectors-master) into the async connector
architecture. No new dependencies — uses httpx, stdlib html.parser, and
xml.etree.ElementTree.

Config keys:
    urls: list[str]              — required, seed URLs to fetch
    scrape_method: str           — "direct" (default), "sitemap", or "recursive"
    max_pages: int               — max pages for sitemap/recursive (default 50)
    max_depth: int               — max depth for recursive crawling (default 2)
    render_js: bool              — reserved for future Playwright integration
    concurrency: int             — max concurrent fetches (default 3)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import StringIO
from urllib.parse import urljoin, urlparse

import httpx
from platform_core.telemetry import get_logger

from platform_knowledge_engine.connectors._utils.ssrf import url_is_safe

from platform_knowledge_engine.connectors.base import (
    ConnectorBase,
    ConnectorTransientError,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = get_logger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_TIMEOUT = 30.0
_MAX_REDIRECTS = 5
_MAX_RETRIES = 3
_RETRY_BACKOFF = [1.0, 2.0, 4.0]
_DEFAULT_MAX_PAGES = 50
_DEFAULT_MAX_DEPTH = 2
_DEFAULT_CONCURRENCY = 3

# Realistic browser User-Agents for rotation (avoids bot detection)
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

# File extensions to skip during recursive crawling
_SKIP_EXTENSIONS = frozenset({
    ".css", ".js", ".json", ".xml", ".rss", ".atom",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".avif",
    ".pdf", ".zip", ".tar", ".gz", ".mp3", ".mp4", ".webm", ".woff", ".woff2",
    ".ttf", ".eot", ".map", ".min.js", ".min.css",
})


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _url_to_id(url: str) -> str:
    """Deterministic external_id from URL so re-syncs don't duplicate."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _title_from_url(url: str) -> str:
    """Best-effort page title from URL path."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path:
        return path.rsplit("/", 1)[-1] or parsed.netloc
    return parsed.netloc


def _same_domain(url1: str, url2: str) -> bool:
    """Check if two URLs share the same registered domain."""
    return urlparse(url1).netloc == urlparse(url2).netloc


def _should_skip_url(url: str) -> bool:
    """Skip non-content URLs (stylesheets, images, scripts, etc.) and, for
    SSRF safety (knowledge-engine S1), any URL that resolves to a private /
    internal / metadata address."""
    if not url_is_safe(url):
        return True
    parsed = urlparse(url)
    path = parsed.path.lower()
    return any(path.endswith(ext) for ext in _SKIP_EXTENSIONS)


# ─── Blocking Detection ──────────────────────────────────────────────────────

def _detect_blocking(status_code: int, text: str, content_length: int) -> str | None:
    """Detect if a response indicates blocking. Returns block type or None.

    Ported from EnhancedWebScraper.WebScrapingDetector.
    """
    text_lower = text.lower() if text else ""

    # Rate limited
    if status_code == 429:
        return "rate_limited"

    # Cloudflare protection
    cf_patterns = ["cf-browser-verification", "cloudflare", "ray id", "cf-chl-bypass"]
    if any(p in text_lower for p in cf_patterns):
        return "cloudflare"

    # CAPTCHA challenge
    captcha_patterns = ["captcha", "recaptcha", "hcaptcha", "g-recaptcha", "cf-turnstile"]
    if any(p in text_lower for p in captcha_patterns):
        return "captcha"

    # Login / auth required
    if status_code in (401, 403):
        login_patterns = ["login", "sign in", "sign-in", "authenticate", "log in"]
        if any(p in text_lower for p in login_patterns):
            return "login_required"
        return "blocked"

    # Bot detection
    bot_patterns = ["bot detection", "automated", "suspicious activity", "access denied",
                    "unusual traffic", "please verify you are human"]
    if any(p in text_lower for p in bot_patterns):
        return "bot_detection"

    # JavaScript required (minimal content returned)
    if content_length < 500:
        js_patterns = ["enable javascript", "javascript is required", "noscript",
                       "this site requires javascript", "please enable javascript"]
        if any(p in text_lower for p in js_patterns):
            return "javascript_required"

    # Minimal content (likely blocked or empty SPA shell)
    if content_length < 200 and status_code == 200:
        return "minimal_content"

    return None


# ─── Enhanced HTML Text Extractor ─────────────────────────────────────────────

class _HTMLTextExtractor(HTMLParser):
    """HTML → text extractor with enhanced cleanup.

    Skips navigation, footer, sidebar, and hidden elements.
    Handles tables (cell → tab), lists (li → bullet), and metadata.

    CR-056: Preserves three classes of metadata as inline markdown so
    they survive into the embedded text — without changing the
    downstream chunker / embedding contract:

      * ``<img alt="X" src="Y">``  →  ``![X](Y)``  (alt text becomes
        searchable; the URL travels with the chunk so the LLM can
        surface it as a clickable image reference).
      * ``<a href="Y">X</a>``      →  ``[X](Y)``   (preserves anchor
        text AND the destination URL; previously only the anchor text
        survived and the href was dropped, so users were told to
        "see the 'learn more' link" with no way to follow it).
      * ``<h1..h6>X</h1..h6>``     →  ``# X`` … ``###### X``
        (gives the chunker a markdown-style boundary to split on; the
        prose text is unchanged).

    Existing behaviour for prose, lists, tables, and chrome filtering
    is unchanged. The enrichment is purely additive — chunks become
    slightly longer (alt-text + URL bytes) but stay well inside any
    realistic max-chunk size, and the embedding model treats the new
    inline syntax as plain text. No schema / API / chart / operator
    changes are needed; only freshly-synced sources pick up the
    enrichment, so existing scraped sources are untouched until they
    get re-synced.
    """

    # Elements whose entire content is skipped
    _SKIP_TAGS = frozenset({
        "script", "style", "noscript", "svg", "head",
        "nav", "footer", "aside", "header", "symbol",
    })

    # CSS classes that indicate non-content elements
    _SKIP_CLASSES = frozenset({
        "sidebar", "footer", "nav", "navigation", "menu",
        "hidden", "sticky", "cookie", "banner", "popup", "modal",
        "ads", "advertisement", "social", "share",
    })

    # Hrefs we should NOT preserve — these are non-navigation pseudo-
    # protocols that would make the markdown link useless or actively
    # leak data (mailto:, tel:) when the chunk is shown to the LLM.
    _SKIP_HREF_PREFIXES = ("#", "mailto:", "tel:", "javascript:")

    def __init__(self, base_url: str = "") -> None:
        super().__init__()
        self._result = StringIO()
        self._skip_depth = 0
        self._title_parts: list[str] = []
        self._description = ""
        self._in_title = False
        self._in_list_item = False

        # Base URL for resolving relative <img src> / <a href>. Empty
        # string disables resolution (the extractor still works; URLs
        # just stay relative). Set via _html_to_text(html, base_url=…).
        self._base_url = base_url or ""

        # ── Anchor handling (CR-056) ──
        # When inside an <a href>, divert text writes into
        # ``_anchor_text`` instead of ``_result`` so we can emit
        # ``[anchor text](href)`` on </a>. ``_anchor_depth`` handles
        # the (technically invalid but seen-in-the-wild) case of
        # nested anchors — only the outermost gets a markdown wrapper
        # so we don't produce garbled brackets.
        self._anchor_href: str | None = None
        self._anchor_text = StringIO()
        self._anchor_depth = 0

    # Internal: write text to whichever sink is active. Anchors divert
    # writes so we can emit a markdown link on close; everything else
    # goes straight to the result buffer.
    def _write(self, s: str) -> None:
        if self._anchor_depth > 0:
            self._anchor_text.write(s)
        else:
            self._result.write(s)

    def _resolve(self, url: str) -> str:
        """Resolve a possibly-relative URL against the base URL.

        Falls back to the original string when ``base_url`` wasn't
        provided or urljoin fails. Never raises — a bad URL is still
        better than no URL at all in the persisted text.
        """
        url = (url or "").strip()
        if not url or not self._base_url:
            return url
        try:
            return urljoin(self._base_url, url)
        except Exception:
            return url

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr_dict = dict(attrs)

        # Skip by tag name
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return

        # Skip by CSS class
        classes = (attr_dict.get("class") or "").lower().split()
        if any(cls in self._SKIP_CLASSES for cls in classes):
            self._skip_depth += 1
            return

        # Skip hidden elements
        style = (attr_dict.get("style") or "").lower()
        if "display:none" in style.replace(" ", "") or "visibility:hidden" in style.replace(" ", ""):
            self._skip_depth += 1
            return

        if tag == "title":
            self._in_title = True

        # Extract meta description
        if tag == "meta":
            name = (attr_dict.get("name") or "").lower()
            if name == "description":
                self._description = attr_dict.get("content", "")

        # Headings — emit a markdown ATX heading on open. The closing
        # newline is written by handle_endtag so the heading sits on
        # its own line. Done BEFORE the generic block-element newline
        # below so we don't get a stray leading blank.
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._write("\n\n" + ("#" * level) + " ")
            return

        # Block-level elements get newlines
        if tag in ("p", "br", "div", "tr", "blockquote", "pre", "section", "article"):
            self._write("\n")

        # List items get bullet prefix
        if tag == "li":
            self._write("\n- ")
            self._in_list_item = True

        # Table cells get tab separator
        if tag in ("td", "th"):
            self._write("\t")

        # Anchor — open a divert. Skip the wrapper for hrefs that are
        # non-navigation (#, mailto:, tel:, javascript:) — the anchor
        # text still flows through normally for those.
        if tag == "a":
            href = (attr_dict.get("href") or "").strip()
            wrap = bool(href) and not href.startswith(self._SKIP_HREF_PREFIXES)
            if wrap and self._anchor_depth == 0:
                # Outermost anchor with a real href — divert text
                self._anchor_href = self._resolve(href)
                self._anchor_text = StringIO()
                self._anchor_depth = 1
            elif self._anchor_depth > 0:
                # Nested anchor — track the depth but do NOT replace
                # the outer sink, so brackets only appear once.
                self._anchor_depth += 1
            return

        # Image — emit ``![alt](src)`` immediately. <img> is a void
        # element so there's no end tag to pair with. Skip when src
        # is empty / data: (inline data URIs are typically tracking
        # pixels or icons and don't help retrieval).
        if tag == "img":
            src = (attr_dict.get("src") or "").strip()
            if not src or src.startswith("data:"):
                return
            alt = (attr_dict.get("alt") or "").strip()
            # Sanitize alt text for markdown — strip ``]`` so it can't
            # close the bracket early. URL is left as-is; bare URLs
            # inside ``()`` parse fine in every common renderer.
            alt_safe = alt.replace("]", "")
            resolved_src = self._resolve(src)
            self._write(f" ![{alt_safe}]({resolved_src}) ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return

        # Also decrement for class-based skips (approximate — handles most cases)
        if tag in ("div", "section", "aside", "nav", "footer", "header") and self._skip_depth > 0:
            self._skip_depth -= 1
            return

        if tag == "title":
            self._in_title = False
        if tag == "li":
            self._in_list_item = False
        if tag == "tr":
            self._write("\n")
        # Headings — flush a trailing newline so the next block starts
        # on its own line (mirrors the leading "\n\n" in handle_starttag).
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._write("\n")

        # Anchor close — if we diverted, emit the markdown link now.
        # The buffer's text is sanitized for stray ``]`` for the same
        # reason as alt text above.
        if tag == "a" and self._anchor_depth > 0:
            self._anchor_depth -= 1
            if self._anchor_depth == 0 and self._anchor_href is not None:
                anchor = self._anchor_text.getvalue().strip().replace("]", "")
                href = self._anchor_href
                # Reset BEFORE writing so _write goes to _result, not
                # the now-stale anchor sink.
                self._anchor_href = None
                self._anchor_text = StringIO()
                if anchor:
                    self._result.write(f"[{anchor}]({href})")
                else:
                    # Empty anchor (e.g. icon-only links) — at least
                    # keep the URL so the LLM can mention it.
                    self._result.write(f"({href})")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data.strip())
        if self._skip_depth == 0:
            self._write(data)

    def get_text(self) -> str:
        raw = self._result.getvalue()
        # Remove zero-width spaces
        raw = raw.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
        # Collapse tabs to single space (from table cell handling)
        raw = re.sub(r"\t+", "\t", raw)
        # Collapse whitespace: multiple spaces → one
        raw = re.sub(r"[ \t]+", " ", raw)
        # Collapse excessive newlines
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        # Clean up lines
        lines = [line.strip() for line in raw.split("\n")]
        return "\n".join(lines).strip()

    def get_title(self) -> str:
        return " ".join(self._title_parts).strip()

    def get_description(self) -> str:
        return self._description


def _html_to_text(html_bytes: bytes, encoding: str = "utf-8", base_url: str = "") -> tuple[str, str]:
    """Extract plain text and title from HTML. Returns (text, title).

    ``base_url`` is used to resolve relative ``<img src>`` and
    ``<a href>`` URLs into absolute URLs so the markdown links the
    extractor now emits stay clickable. Optional — when empty, URLs
    are left as written in the source HTML (CR-056).
    """
    try:
        html = html_bytes.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = html_bytes.decode("utf-8", errors="replace")
    extractor = _HTMLTextExtractor(base_url=base_url)
    extractor.feed(html)
    return extractor.get_text(), extractor.get_title()


def _extract_links(html_bytes: bytes, base_url: str, encoding: str = "utf-8") -> list[str]:
    """Extract all <a href> links from HTML, resolved to absolute URLs."""

    class _LinkExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.links: list[str] = []

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag == "a":
                href = dict(attrs).get("href", "")
                if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                    absolute = urljoin(base_url, href)
                    # Strip fragment
                    absolute = absolute.split("#")[0]
                    if absolute.startswith("http"):
                        self.links.append(absolute)

    try:
        html = html_bytes.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = html_bytes.decode("utf-8", errors="replace")
    parser = _LinkExtractor()
    parser.feed(html)
    return parser.links


# ─── Sitemap Parser ───────────────────────────────────────────────────────────

async def _fetch_sitemap_urls(client: httpx.AsyncClient, base_url: str, max_pages: int) -> list[str]:
    """Fetch and parse sitemap.xml, returning up to max_pages URLs."""
    parsed = urlparse(base_url)
    sitemap_urls_to_try = [
        f"{parsed.scheme}://{parsed.netloc}/sitemap.xml",
        f"{parsed.scheme}://{parsed.netloc}/sitemap_index.xml",
        f"{parsed.scheme}://{parsed.netloc}/robots.txt",
    ]

    urls: list[str] = []

    for sitemap_url in sitemap_urls_to_try:
        try:
            resp = await client.get(sitemap_url, headers={"User-Agent": _random_ua()})
            if resp.status_code != 200:
                continue

            content = resp.text

            # robots.txt — extract Sitemap: directives
            if sitemap_url.endswith("robots.txt"):
                for line in content.split("\n"):
                    if line.strip().lower().startswith("sitemap:"):
                        sm_url = line.split(":", 1)[1].strip()
                        sub_urls = await _parse_sitemap_xml(client, sm_url, max_pages - len(urls))
                        urls.extend(sub_urls)
                        if len(urls) >= max_pages:
                            return urls[:max_pages]
                continue

            # XML sitemap
            sub_urls = _parse_sitemap_content(content, max_pages - len(urls))
            urls.extend(sub_urls)
            if urls:
                break  # Got URLs from this sitemap, done

        except Exception as e:
            logger.debug("sitemap_fetch_failed", url=sitemap_url, error=str(e))
            continue

    return urls[:max_pages]


async def _parse_sitemap_xml(client: httpx.AsyncClient, url: str, max_pages: int) -> list[str]:
    """Fetch and parse a single sitemap XML file."""
    try:
        resp = await client.get(url, headers={"User-Agent": _random_ua()})
        if resp.status_code != 200:
            return []
        return _parse_sitemap_content(resp.text, max_pages)
    except Exception:
        return []


def _parse_sitemap_content(xml_content: str, max_pages: int) -> list[str]:
    """Parse sitemap XML content and extract URLs."""
    urls: list[str] = []
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    # Handle namespace (sitemaps use {http://www.sitemaps.org/schemas/sitemap/0.9})
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    # <url><loc>...</loc></url> entries
    for url_el in root.findall(f".//{ns}loc"):
        if url_el.text:
            urls.append(url_el.text.strip())
            if len(urls) >= max_pages:
                break

    return urls


# ─── WebConnector ─────────────────────────────────────────────────────────────

class WebConnector(ConnectorBase):
    """Enhanced web connector with blocking detection, sitemap discovery,
    recursive crawling, User-Agent rotation, and retry with backoff.

    Config:
        urls: list[str]              — required, seed URLs to fetch
        scrape_method: str           — "direct" (default), "sitemap", or "recursive"
        max_pages: int               — max pages for sitemap/recursive (default 50)
        max_depth: int               — max depth for recursive crawling (default 2)
        concurrency: int             — max concurrent fetches (default 3)
    """

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        raw_urls: list[str] = self._config.get("urls") or []
        # SSRF protection: validate all user-provided URLs before storing
        self._urls: list[str] = []
        for url in raw_urls:
            try:
                from platform_core.middleware.url_validator import validate_url
                validated = validate_url(url)
                self._urls.append(validated)
            except Exception as exc:
                # SSRF validator may fail inside K8s pods due to DNS resolution
                # (e.g. external hostnames resolving to private IPs via cluster DNS).
                # Fall back to basic scheme/hostname validation so legitimate
                # external URLs are not silently dropped.
                logger.warning("web_connector_ssrf_check_failed: url=%s reason=%s — applying fallback validation", url, exc)
                try:
                    parsed = urlparse(url.strip())
                    if parsed.scheme in ("http", "https") and parsed.hostname:
                        self._urls.append(url.strip())
                        logger.info("web_connector_url_accepted_via_fallback: url=%s", url)
                    else:
                        logger.warning("web_connector_url_blocked: url=%s reason=invalid scheme or hostname", url)
                except Exception:
                    logger.warning("web_connector_url_blocked: url=%s reason=%s", url, exc)
        self._scrape_method: str = self._config.get("scrape_method", "direct")
        self._max_pages: int = int(self._config.get("max_pages", _DEFAULT_MAX_PAGES))
        self._max_depth: int = int(self._config.get("max_depth", _DEFAULT_MAX_DEPTH))
        self._concurrency: int = int(self._config.get("concurrency", _DEFAULT_CONCURRENCY))
        self._client: httpx.AsyncClient | None = None
        self._discovered_urls: dict[str, str] = {}  # doc_id → url

    async def authenticate(self, credentials: dict) -> None:
        """No authentication needed for public web pages."""

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # trust_env=True is httpx's default (reads HTTP_PROXY/HTTPS_PROXY/
            # NO_PROXY) — stated explicitly so it can't be disabled by a future
            # refactor without a reviewer noticing. Log the effective proxy on
            # first construction so "no healthy upstream" failures are
            # immediately traceable to the envoy/istio sidecar vs a user-set
            # proxy envvar.
            import os
            http_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
            no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
            if http_proxy:
                logger.info(
                    "web_connector_using_proxy",
                    http_proxy=http_proxy,
                    no_proxy=no_proxy,
                )
            self._client = httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                headers={"User-Agent": _random_ua()},
                trust_env=True,
            )
        return self._client

    # ── Document Discovery ────────────────────────────────────────────────

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Discover URLs based on scrape_method and yield DocumentMetadata."""
        discovered: list[str] = []

        if self._scrape_method == "sitemap":
            discovered = await self._discover_sitemap()
        elif self._scrape_method == "recursive":
            discovered = await self._discover_recursive()
        else:
            # Direct mode — just use configured URLs
            discovered = [u.strip() for u in self._urls if u.strip()]

        for url in discovered:
            # SEC (knowledge-engine S1): never yield/ingest a URL that resolves
            # to an internal/metadata address.
            if not url_is_safe(url):
                logger.warning("web_connector_skip_unsafe_url", url=url)
                continue
            doc_id = _url_to_id(url)
            self._discovered_urls[doc_id] = url
            yield DocumentMetadata(
                external_id=doc_id,
                title=_title_from_url(url),
                url=url,
                content_type="text/plain",
                modified_at=datetime.now(UTC),
                metadata={"url": url},
            )

    async def _discover_sitemap(self) -> list[str]:
        """Discover URLs from sitemap.xml for all seed URLs."""
        client = await self._get_client()
        all_urls: list[str] = []
        seen: set[str] = set()

        for seed in self._urls:
            seed = seed.strip()
            if not seed:
                continue
            # SEC (knowledge-engine S1): skip SSRF-unsafe seed URLs.
            if not url_is_safe(seed):
                logger.warning("web_connector_skip_unsafe_seed", url=seed)
                continue
            try:
                sitemap_urls = await _fetch_sitemap_urls(client, seed, self._max_pages - len(all_urls))
                for url in sitemap_urls:
                    if url not in seen:
                        seen.add(url)
                        all_urls.append(url)
            except Exception as e:
                logger.warning("sitemap_discovery_failed", url=seed, error=str(e))

            if len(all_urls) >= self._max_pages:
                break

        # Always include seed URLs even if sitemap didn't list them
        for seed in self._urls:
            seed = seed.strip()
            if seed and seed not in seen:
                seen.add(seed)
                all_urls.append(seed)

        logger.info("sitemap_discovery_complete", urls_found=len(all_urls))
        return all_urls[:self._max_pages]

    async def _discover_recursive(self) -> list[str]:
        """Discover URLs by recursive crawling from seed URLs (BFS)."""
        client = await self._get_client()
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque()  # (url, depth)
        result: list[str] = []

        for seed in self._urls:
            seed = seed.strip()
            if seed:
                queue.append((seed, 0))
                visited.add(seed)

        while queue and len(result) < self._max_pages:
            url, depth = queue.popleft()
            # SEC (knowledge-engine S1): don't fetch/record SSRF-unsafe URLs.
            if not url_is_safe(url):
                logger.warning("web_connector_skip_unsafe_url", url=url)
                continue
            result.append(url)

            if depth >= self._max_depth:
                continue

            # Fetch page and extract links
            try:
                resp = await client.get(url, headers={"User-Agent": _random_ua()})
                if resp.status_code != 200:
                    continue

                encoding = "utf-8"
                ct = resp.headers.get("content-type", "")
                if "charset=" in ct:
                    encoding = ct.split("charset=")[-1].split(";")[0].strip()

                links = _extract_links(resp.content, url, encoding)
                for link in links:
                    if (
                        link not in visited
                        and _same_domain(url, link)
                        and not _should_skip_url(link)
                        and len(link) < 500
                    ):
                        visited.add(link)
                        queue.append((link, depth + 1))

                # Brief delay to be polite
                await asyncio.sleep(0.5)

            except Exception as e:
                logger.debug("recursive_crawl_link_failed", url=url, error=str(e))

        logger.info("recursive_discovery_complete", urls_found=len(result), max_depth=self._max_depth)
        return result[:self._max_pages]

    # ── Document Fetching ─────────────────────────────────────────────────

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a web page with retry, backoff, and blocking detection.

        Uses User-Agent rotation and exponential backoff on failures.
        """
        url = self._discovered_urls.get(doc_id) or self._find_url(doc_id)
        if not url:
            raise FileNotFoundError(f"No URL found for document {doc_id} in source config")

        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                client = await self._get_client()
                # Rotate UA on each attempt
                headers = {"User-Agent": _random_ua()}
                resp = await client.get(url, headers=headers)

                # Check for blocking before raising for status
                text_preview = ""
                try:
                    text_preview = resp.text[:2000]
                except Exception:
                    pass

                block_type = _detect_blocking(resp.status_code, text_preview, len(resp.content))
                if block_type:
                    logger.warning(
                        "web_fetch_blocked",
                        url=url,
                        block_type=block_type,
                        status_code=resp.status_code,
                        attempt=attempt + 1,
                    )
                    if block_type == "rate_limited":
                        retry_after = int(resp.headers.get("retry-after", _RETRY_BACKOFF[attempt]))
                        await asyncio.sleep(retry_after)
                        continue
                    if block_type in ("cloudflare", "captcha", "bot_detection"):
                        await asyncio.sleep(_RETRY_BACKOFF[attempt] + random.uniform(0, 1))
                        continue
                    if block_type == "javascript_required":
                        logger.warning("web_fetch_js_required", url=url)
                        # Can't retry — JS rendering needed; return what we have
                        break
                    if block_type == "minimal_content":
                        # Might just be a light page; use what we got
                        break

                resp.raise_for_status()

                # Success — extract text
                encoding = "utf-8"
                ct = resp.headers.get("content-type", "")
                if "charset=" in ct:
                    encoding = ct.split("charset=")[-1].split(";")[0].strip()

                # Pass the page URL as ``base_url`` so the extractor
                # can resolve relative ``<img src>`` / ``<a href>``
                # values into absolute URLs in the emitted markdown
                # (CR-056). Without this the LLM gets ``/foo/bar.png``
                # and can't open it; with it the LLM gets the full
                # ``https://host/foo/bar.png``.
                text, page_title = _html_to_text(resp.content, encoding, base_url=url)

                return RawDocument(
                    external_id=doc_id,
                    content=text.encode("utf-8"),
                    content_type="text/plain",
                    metadata={
                        "url": url,
                        "page_title": page_title or _title_from_url(url),
                        "original_filename": (page_title or _title_from_url(url)) + ".txt",
                        "status_code": resp.status_code,
                        "content_length": len(text),
                    },
                )

            except httpx.HTTPStatusError as e:
                last_error = e
                # "no healthy upstream" is Envoy/Istio speak for "the sidecar
                # could not reach any backend for this host" — typically means
                # the pod has an istio sidecar but no ServiceEntry for the
                # external domain, or egress is network-policy-blocked. Flag
                # it distinctly so the next operator on-call doesn't have to
                # rediscover the cluster-network root cause.
                body_preview = ""
                try:
                    body_preview = e.response.text[:300]
                except Exception:
                    pass
                mesh_block = (
                    e.response.status_code == 503
                    and "no healthy upstream" in body_preview.lower()
                )
                logger.warning(
                    "web_fetch_http_error",
                    url=url,
                    status=e.response.status_code,
                    attempt=attempt + 1,
                    mesh_block=mesh_block,
                    body_preview=body_preview,
                )
                if mesh_block:
                    raise ConnectorTransientError(
                        f"External fetch blocked by in-cluster mesh (Envoy '503 no healthy upstream') for {url}. "
                        "Check istio ServiceEntry / egress NetworkPolicy for the host.",
                        connector_type="web",
                    ) from e
                if e.response.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(_RETRY_BACKOFF[attempt] + random.uniform(0, 1))
                    continue
                break  # Non-retryable status

            except httpx.RequestError as e:
                last_error = e
                logger.warning(
                    "web_fetch_network_error",
                    url=url,
                    error=str(e),
                    attempt=attempt + 1,
                )
                await asyncio.sleep(_RETRY_BACKOFF[attempt])
                continue

        raise ConnectorTransientError(
            f"Failed to fetch {url} after {_MAX_RETRIES} attempts: {last_error}",
            connector_type="web",
        )

    # ── Permissions ───────────────────────────────────────────────────────

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Grant the public wildcard viewer unless the source is restricted.

        Returning [] used to mean "no access restrictions", but KE retrieval
        is default-deny: _resolve_permissions builds the permitted doc set from
        SpiceDB lookup_resources(user, "view") and pre-filters Milvus by
        document_id. A doc with NO grant is in nobody's permitted set, so every
        web chunk was excluded ("0 candidates -> 0 results, perms=filtered")
        even though the vectors were indexed.

        Crawled web pages are public by nature, so by default we emit the
        SpiceDB `user:*` wildcard viewer (visible to all users; tenant
        isolation still enforced by the PostgreSQL/Milvus/doc_id layers — see
        the document definition in SPICEDB_SCHEMA). BUT if an admin has
        restricted the source via the source-access model (visibility
        "personal" or "teams"; see routes/v1/sources.py), we return [] and let
        the data_source-level ACL govern (document.view inherits data_source->
        view) instead of stamping a public grant that would override the
        restriction.
        """
        visibility = (self._config.get("visibility") or "").lower().strip()
        if visibility in ("personal", "teams"):
            return []
        return [PermissionEntry(subject_type="user", subject_id="*", relation="viewer")]

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Internal ──────────────────────────────────────────────────────────

    def _find_url(self, doc_id: str) -> str | None:
        """Find the URL that matches a doc_id."""
        for url in self._urls:
            url = url.strip()
            if url and _url_to_id(url) == doc_id:
                return url
        return None
