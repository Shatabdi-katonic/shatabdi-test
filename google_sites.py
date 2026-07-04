"""Google Sites connector.

API: Google Sites (via sitemap crawling + page fetch)
Auth: OAuth 2.0 (access_token with Drive API scopes)
Sync: Full crawl (Google Sites has no incremental API)
Permissions: Site-level via Google Drive API — new-editor Sites are stored
             as Drive files, so sharing permissions on the Drive file apply
             to the entire site.  All pages inherit the site's permissions.

Content types indexed:
  - Site pages (HTML content via HTTP fetch with OAuth token)

Note: Google Sites does not have a well-documented REST API for content
retrieval. This connector discovers pages via the site's sitemap.xml and
fetches each page's HTML using the authenticated session.  Permission
awareness is achieved by querying the Google Drive API for the underlying
Site file's sharing settings.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from xml.etree import ElementTree

from platform_knowledge_engine.connectors._utils.http_client import (
    RetryClient,
    bearer_headers,
)
from platform_knowledge_engine.connectors.base import (
    ConnectorAuthError,
    ConnectorBase,
    ConnectorRateLimitError,
    ConnectorTransientError,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)

# Sitemap XML namespace
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Google Drive API v3 base URL (used for permission lookups)
DRIVE_API = "https://www.googleapis.com/drive/v3"

# Drive permission role → normalised relation
_ROLE_MAP: dict[str, str] = {
    "owner": "owner",
    "organizer": "owner",
    "fileOrganizer": "editor",
    "writer": "editor",
    "commenter": "viewer",
    "reader": "viewer",
}


class GoogleSitesConnector(ConnectorBase):
    """Google Sites connector that crawls site pages via sitemap."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._site_url: str = config.get("site_url", "").rstrip("/")
        self._client: RetryClient | None = None
        self._drive_client: RetryClient | None = None
        self._access_token: str = ""
        self._site_permissions: list[PermissionEntry] | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using an OAuth access_token (e.g. from OAuth flow).

        Expects credentials: {access_token: str, site_url?: str}
        """
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError(
                "Google Sites requires access_token",
                connector_type="google_sites",
            )

        if not self._site_url:
            site_url = credentials.get("site_url", "")
            if not site_url:
                raise ConnectorAuthError(
                    "Google Sites requires site_url in config or credentials",
                    connector_type="google_sites",
                )
            self._site_url = site_url.rstrip("/")

        self._access_token = token

        self._client = RetryClient(
            headers={
                **bearer_headers(token),
                "Accept": "text/html, application/xml",
            },
            timeout=30.0,
        )

        self._drive_client = RetryClient(
            base_url=DRIVE_API,
            headers={
                **bearer_headers(token),
                "Accept": "application/json",
            },
            timeout=30.0,
        )

        # Verify connectivity by fetching the site root
        try:
            resp = await self._client.get(self._site_url)
            if resp.status_code >= 400:
                raise ConnectorAuthError(
                    f"Cannot access site: HTTP {resp.status_code}",
                    connector_type="google_sites",
                )
            logger.info("Google Sites authenticated for %s", self._site_url)
        except ConnectorAuthError:
            raise
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"Google Sites authentication failed: {e}",
                connector_type="google_sites",
            ) from e

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Discover pages via sitemap.xml and yield metadata.

        Falls back to crawling the site root if no sitemap is found.
        """
        assert self._client is not None

        pages = await self._discover_pages()

        for page_url, lastmod in pages:
            modified_at = lastmod or datetime.now(UTC)
            if since and modified_at <= since:
                continue

            # Derive a stable ID from the URL path
            page_id = _url_to_id(page_url, self._site_url)
            title = _url_to_title(page_url)

            yield DocumentMetadata(
                external_id=page_id,
                title=title,
                url=page_url,
                content_type="text/html",
                modified_at=modified_at,
                metadata={"source_url": page_url},
            )

    async def _discover_pages(self) -> list[tuple[str, datetime | None]]:
        """Discover page URLs from sitemap.xml or robots.txt."""
        assert self._client is not None
        pages: list[tuple[str, datetime | None]] = []

        # Try sitemap.xml first
        sitemap_url = f"{self._site_url}/sitemap.xml"
        try:
            resp = await self._client.get(sitemap_url)
            if resp.status_code == 200:
                pages = _parse_sitemap(resp.text)
                if pages:
                    logger.info("Discovered %d pages from sitemap", len(pages))
                    return pages
        except Exception:
            logger.debug("No sitemap.xml found, falling back to root crawl")

        # Fallback: fetch the root page and extract internal links
        try:
            resp = await self._client.get(self._site_url)
            if resp.status_code == 200:
                pages = _extract_links(resp.text, self._site_url)
                # Always include the root page itself
                pages.insert(0, (self._site_url, None))
                logger.info("Discovered %d pages via link crawl", len(pages))
        except Exception as e:
            logger.warning("Failed to crawl site root: %s", e)

        return pages

    # ------------------------------------------------------------------
    # Fetch document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a page's HTML content by reconstructing its URL."""
        assert self._client is not None

        page_url = _id_to_url(doc_id, self._site_url)

        try:
            resp = await self._client.get(page_url)
        except Exception as e:
            _raise_typed(e, "google_sites")

        html = resp.text
        title = _extract_title_from_html(html)

        return RawDocument(
            external_id=doc_id,
            content=html.encode("utf-8"),
            content_type="text/html",
            metadata={"title": title, "url": page_url},
        )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def _fetch_site_permissions(self) -> list[PermissionEntry]:
        """Fetch site-level permissions via the Google Drive API.

        Google Sites (new editor) are stored as Drive files with MIME type
        ``application/vnd.google-apps.site``.  We search for the file whose
        ``webViewLink`` matches ``self._site_url``, then read its sharing
        permissions.  The result is cached for the lifetime of the connector
        instance so that we only hit the Drive API once per sync run.
        """
        if self._site_permissions is not None:
            return self._site_permissions

        assert self._drive_client is not None

        # 1. Find the Drive file ID for this site
        file_id = await self._resolve_site_file_id()
        if not file_id:
            logger.warning(
                "Could not find Drive file for site %s – returning empty permissions",
                self._site_url,
            )
            self._site_permissions = []
            return self._site_permissions

        # 2. Fetch permissions on that file
        try:
            resp = await self._drive_client.get(
                f"/files/{file_id}/permissions",
                params={
                    "fields": "permissions(id,type,emailAddress,role,domain)",
                    "supportsAllDrives": "true",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Drive permissions lookup failed: %s", exc)
            self._site_permissions = []
            return self._site_permissions

        # 3. Map Drive permissions → PermissionEntry
        entries: list[PermissionEntry] = []
        for perm in data.get("permissions", []):
            entry = _drive_perm_to_entry(perm)
            if entry is not None:
                entries.append(entry)

        logger.info(
            "Resolved %d permission entries for site %s",
            len(entries),
            self._site_url,
        )
        self._site_permissions = entries
        return self._site_permissions

    async def _resolve_site_file_id(self) -> str | None:
        """Search Google Drive for a Site file matching ``self._site_url``."""
        assert self._drive_client is not None

        try:
            resp = await self._drive_client.get(
                "/files",
                params={
                    "q": "mimeType='application/vnd.google-apps.site'",
                    "fields": "files(id,name,webViewLink)",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    "corpora": "allDrives",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Drive file search failed: %s", exc)
            return None

        normalised_site = self._site_url.rstrip("/").lower()
        for f in data.get("files", []):
            link = (f.get("webViewLink") or "").rstrip("/").lower()
            if link == normalised_site:
                logger.debug("Matched Drive file %s (%s)", f["id"], f.get("name"))
                return f["id"]

        logger.debug(
            "No Drive file matched %s among %d candidates",
            self._site_url,
            len(data.get("files", [])),
        )
        return None

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Return site-level permissions — all pages inherit from the site."""
        return await self._fetch_site_permissions()

    async def get_folder_permissions(self, folder_id: str) -> list[PermissionEntry]:
        """Return site-level permissions (sites have a flat page hierarchy)."""
        return await self._fetch_site_permissions()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            resp = await self._client.get(self._site_url)
            return resp.status_code < 400
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()
        if self._drive_client:
            await self._drive_client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drive_perm_to_entry(perm: dict) -> PermissionEntry | None:
    """Convert a single Google Drive permission object to a PermissionEntry.

    Returns ``None`` for permission types we intentionally skip (e.g. "anyone").
    """
    perm_type = perm.get("type", "")
    role = perm.get("role", "reader")
    relation = _ROLE_MAP.get(role, "viewer")

    if perm_type == "user":
        email = perm.get("emailAddress", "")
        if not email:
            return None
        return PermissionEntry(subject_type="user", subject_id=email, relation=relation)

    if perm_type == "group":
        email = perm.get("emailAddress", "")
        if not email:
            return None
        return PermissionEntry(subject_type="group", subject_id=email, relation=relation)

    if perm_type == "domain":
        domain = perm.get("domain", "")
        if not domain:
            return None
        return PermissionEntry(subject_type="domain", subject_id=domain, relation=relation)

    # "anyone" — public access; skip by default
    return None


def _parse_sitemap(xml_text: str) -> list[tuple[str, datetime | None]]:
    """Parse a sitemap.xml and return (url, lastmod) tuples."""
    pages: list[tuple[str, datetime | None]] = []
    try:
        root = ElementTree.fromstring(xml_text)
        for url_elem in root.findall("sm:url", SITEMAP_NS):
            loc = url_elem.findtext("sm:loc", default="", namespaces=SITEMAP_NS)
            lastmod_str = url_elem.findtext("sm:lastmod", default="", namespaces=SITEMAP_NS)
            lastmod = _parse_dt(lastmod_str) if lastmod_str else None
            if loc:
                pages.append((loc, lastmod))
    except ElementTree.ParseError:
        pass
    return pages


def _extract_links(html: str, base_url: str) -> list[tuple[str, datetime | None]]:
    """Extract same-site links from HTML using a simple regex."""
    pattern = re.compile(r'href=["\'](' + re.escape(base_url) + r'[^"\']*)["\']')
    seen: set[str] = set()
    pages: list[tuple[str, datetime | None]] = []
    for match in pattern.finditer(html):
        url = match.group(1).split("#")[0].split("?")[0]
        if url not in seen:
            seen.add(url)
            pages.append((url, None))
    return pages


def _extract_title_from_html(html: str) -> str:
    """Extract <title> from HTML."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else "Untitled"


def _url_to_id(url: str, base_url: str) -> str:
    """Derive a stable page ID from URL by stripping the base."""
    path = url.replace(base_url, "").strip("/")
    return path or "index"


def _id_to_url(page_id: str, base_url: str) -> str:
    """Reconstruct a page URL from its ID."""
    if page_id == "index":
        return base_url
    return f"{base_url}/{page_id}"


def _url_to_title(url: str) -> str:
    """Derive a human-readable title from a URL path."""
    path = url.rstrip("/").rsplit("/", 1)[-1]
    return path.replace("-", " ").replace("_", " ").title() or "Home"


def _parse_dt(s: str) -> datetime:
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _raise_typed(exc: Exception, connector_type: str) -> None:
    """Re-raise as the appropriate ConnectorError subclass."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            raise ConnectorAuthError(str(exc), connector_type=connector_type) from exc
        if code == 429:
            retry_after = float(exc.response.headers.get("Retry-After", "5"))
            raise ConnectorRateLimitError(
                str(exc), connector_type=connector_type, retry_after=retry_after
            ) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    raise exc
