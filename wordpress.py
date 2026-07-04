"""WordPress connector.

API: WordPress REST API v2 (wp-json)
Auth: Bearer access_token (OAuth 2.0) or Application Passwords
Sync: Incremental (modified_after filter)
Permissions: Not supported
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers
from platform_knowledge_engine.connectors.base import (
    ConnectorAuthError, ConnectorBase, ConnectorRateLimitError,
    ConnectorTransientError, DocumentMetadata, PermissionEntry, RawDocument,
)

logger = logging.getLogger(__name__)


class WordPressConnector(ConnectorBase):
    """Native WordPress connector via REST API.

    Config:
        site_url: WordPress site URL (e.g. https://example.com)
        content_types: List of post types to sync (default: posts, pages)
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept the canonical key (`site_url`) and the AddKnowledgeModal form
        # key (`wordpressSiteUrl`); normalise the scheme. Same field-mapping
        # fallback pattern as the Outline / ClickUp / Linear connectors.
        self._site_url: str = self._ensure_scheme(
            config.get("site_url") or config.get("wordpressSiteUrl") or ""
        )
        self._content_types: list[str] = config.get("content_types", ["posts", "pages"])
        self._client: RetryClient | None = None

    @staticmethod
    def _ensure_scheme(url: str) -> str:
        """Prepend https:// when the user-entered site URL omits a scheme."""
        url = (url or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

    @staticmethod
    def _api_base(site_url: str) -> str:
        """Return the WP REST v2 base for the site.

        WordPress.com-hosted sites (``*.wordpress.com``) do NOT expose the
        self-hosted ``/wp-json/wp/v2`` path — it returns 404. They are served
        via the central ``public-api.wordpress.com/wp/v2/sites/{site}``
        endpoint instead. Self-hosted installs use ``{site}/wp-json/wp/v2``.
        Without this split, every WordPress.com source failed auth at
        ``/users/me`` with a 404 even with a valid OAuth token + site_url.
        """
        host = site_url.replace("https://", "").replace("http://", "").rstrip("/")
        if host == "wordpress.com" or host.endswith(".wordpress.com"):
            return f"https://public-api.wordpress.com/wp/v2/sites/{host}"
        return f"{site_url.rstrip('/')}/wp-json/wp/v2"

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        site_url = credentials.get("site_url") or self._site_url
        if not token:
            raise ConnectorAuthError("WordPress requires 'access_token'", connector_type="wordpress")
        if not site_url:
            raise ConnectorAuthError("WordPress requires 'site_url'", connector_type="wordpress")
        self._site_url = self._ensure_scheme(site_url)
        base_url = self._api_base(self._site_url)
        self._client = RetryClient(base_url=base_url, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/users/me")
            me = resp.json()
            logger.info("WordPress authenticated as %s", me.get("name", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"WordPress auth failed: {exc}", connector_type="wordpress") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        for content_type in self._content_types:
            page = 1
            while True:
                params: dict = {"page": page, "per_page": 100, "status": "publish,draft", "orderby": "modified"}
                if since:
                    params["modified_after"] = since.strftime("%Y-%m-%dT%H:%M:%S")
                try:
                    resp = await self._client.get(f"/{content_type}", params=params)
                except Exception as exc:
                    _raise_mapped(exc, "wordpress")
                    raise
                items = resp.json()
                if not items or not isinstance(items, list):
                    break
                for item in items:
                    title = (item.get("title") or {}).get("rendered", "") if isinstance(item.get("title"), dict) else item.get("title", "")
                    yield DocumentMetadata(
                        external_id=f"{content_type}_{item['id']}",
                        title=title,
                        url=item.get("link"),
                        content_type="text/html",
                        modified_at=_parse_ts(item.get("modified_gmt", "")),
                        metadata={"type": content_type, "status": item.get("status")},
                    )
                total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
                if page >= total_pages:
                    break
                page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        parts_split = doc_id.split("_", 1)
        content_type = parts_split[0] if len(parts_split) == 2 else "posts"
        post_id = parts_split[1] if len(parts_split) == 2 else doc_id
        try:
            resp = await self._client.get(f"/{content_type}/{post_id}")
        except Exception as exc:
            _raise_mapped(exc, "wordpress")
            raise
        item = resp.json()
        title = (item.get("title") or {}).get("rendered", "") if isinstance(item.get("title"), dict) else item.get("title", "")
        content_html = (item.get("content") or {}).get("rendered", "") if isinstance(item.get("content"), dict) else item.get("content", "")
        parts = [f"# {title}", ""]
        parts.append(f"**Status:** {item.get('status', '')}")
        parts.append(f"**Date:** {item.get('date_gmt', '')}")
        parts.append("")
        parts.append(content_html)
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/html", metadata={"title": title})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/users/me")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _parse_ts(ts: str) -> datetime:
    if not ts:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _raise_mapped(exc: Exception, connector_type: str) -> None:
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            raise ConnectorAuthError(str(exc), connector_type=connector_type) from exc
        if code == 429:
            raise ConnectorRateLimitError(str(exc), connector_type=connector_type, retry_after=float(exc.response.headers.get("Retry-After", "5"))) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
