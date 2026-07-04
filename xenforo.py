"""XenForo connector.

API: XenForo REST API (threads, posts)
Auth: API key via XF-Api-Key header
Sync: Incremental (last_post_date filter) + full
Permissions: Empty (forum permissions are role-based; not mapped)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

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


class XenForoConnector(ConnectorBase):
    """Native XenForo forum connector via REST API.

    Config:
        base_url: The XenForo forum base URL (e.g. https://forum.example.com).
        max_pages: Maximum number of thread list pages to fetch. Default 50.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept canonical key (`base_url`) and frontend form key
        # (`xenforoUrl`). AddKnowledgeModal sends `xenforoUrl`
        # directly into config; without this fallback the connector
        # raises an "API key requires base_url" auth error or
        # silently lists 0 documents.
        self._base_url: str = (
            config.get("base_url") or config.get("xenforoUrl") or ""
        ).rstrip("/")
        self._max_pages: int = config.get("max_pages", 50)
        # Hold onto a possible config-side API key so authenticate()
        # can fall back when the sync route hasn't materialised it
        # into credentials. API-key XenForo sources created via
        # AddKnowledgeModal carry the key in config under `xenforoApiKey`.
        self._fallback_api_key: str = config.get("xenforoApiKey") or ""
        self._client: httpx.AsyncClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with a XenForo API key.

        Expects credentials with: api_key.
        Optionally: base_url (overrides config).
        """
        # Prefer canonical credentials path. Fall back to the key
        # captured from config in __init__ — that's how API-key
        # XenForo sources created via AddKnowledgeModal carry the
        # API key today.
        api_key = credentials.get("api_key") or self._fallback_api_key or ""
        if not api_key:
            raise ConnectorAuthError(
                "XenForo connector requires api_key", connector_type="xenforo"
            )

        if credentials.get("base_url"):
            self._base_url = credentials["base_url"].rstrip("/")

        if not self._base_url:
            raise ConnectorAuthError(
                "XenForo connector requires base_url in config or credentials",
                connector_type="xenforo",
            )

        from platform_knowledge_engine.connectors._utils.http_client import RetryClient
        self._client = RetryClient(
            base_url=self._base_url,
            headers={"XF-Api-Key": api_key},
            timeout=30.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify the API key by fetching forum info
        try:
            data = await self._get_json("/api/index")
            logger.info(
                "XenForo connector authenticated: %s",
                data.get("title", self._base_url),
            )
        except ConnectorAuthError:
            await self.close()
            raise
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"XenForo API key verification failed: {e}",
                connector_type="xenforo",
            ) from e

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET with standard error handling for XenForo API."""
        assert self._client is not None
        try:
            resp = await self._client.get(url, params=params)
        except httpx.TimeoutException as e:
            raise ConnectorTransientError(
                f"XenForo API timeout: {e}", connector_type="xenforo"
            ) from e

        if resp.status_code in (401, 403):
            raise ConnectorAuthError(
                f"XenForo API auth error: {resp.status_code}",
                connector_type="xenforo",
            )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "10"))
            raise ConnectorRateLimitError(
                "XenForo API rate limited",
                connector_type="xenforo",
                retry_after=retry_after,
            )
        if resp.status_code >= 500:
            raise ConnectorTransientError(
                f"XenForo API server error: {resp.status_code}",
                connector_type="xenforo",
            )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # ConnectorBase implementation
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List forum threads, optionally filtered by last_post_date.

        Paginates through thread listing pages up to ``max_pages``.
        Each thread becomes one document.
        """
        assert self._client is not None

        since_ts = int(since.timestamp()) if since else 0

        for page in range(1, self._max_pages + 1):
            data = await self._get_json("/api/threads", params={"page": str(page)})

            threads = data.get("threads", [])
            if not threads:
                break

            for thread in threads:
                last_post_date = thread.get("last_post_date", 0)

                if since_ts and last_post_date <= since_ts:
                    continue

                modified = datetime.fromtimestamp(last_post_date, tz=UTC)
                thread_id = str(thread.get("thread_id", ""))
                title = thread.get("title", f"Thread {thread_id}")
                username = thread.get("username", "")
                forum = thread.get("Forum", {})
                forum_title = forum.get("title", "") if isinstance(forum, dict) else ""

                yield DocumentMetadata(
                    external_id=thread_id,
                    title=title,
                    url=f"{self._base_url}/threads/{thread_id}/",
                    content_type="text/html",
                    author=username,
                    modified_at=modified,
                    folder_id=str(thread.get("node_id", "")),
                    metadata={
                        "reply_count": thread.get("reply_count", 0),
                        "view_count": thread.get("view_count", 0),
                        "forum_title": forum_title,
                        "is_sticky": thread.get("sticky", False),
                    },
                )

            # Check pagination info
            pagination = data.get("pagination", {})
            if page >= pagination.get("last_page", page):
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a thread and all its posts.

        Serializes the thread title followed by all post bodies in order.
        Paginates through posts if the thread has multiple pages.
        """
        assert self._client is not None

        # Fetch thread metadata
        thread_data = await self._get_json(f"/api/threads/{doc_id}")
        thread = thread_data.get("thread", {})
        title = thread.get("title", f"Thread {doc_id}")

        # Fetch all posts
        parts: list[str] = [f"# {title}\n"]
        page = 1

        while True:
            posts_data = await self._get_json(
                f"/api/threads/{doc_id}/posts", params={"page": str(page)}
            )

            posts = posts_data.get("posts", [])
            if not posts:
                break

            for post in posts:
                username = post.get("username", "unknown")
                post_date = post.get("post_date", 0)
                dt = datetime.fromtimestamp(post_date, tz=UTC)
                body = post.get("message", "")

                parts.append(f"[{username} @ {dt.isoformat()}]\n{body}")

            # Check pagination
            pagination = posts_data.get("pagination", {})
            if page >= pagination.get("last_page", page):
                break
            page += 1

        content = "\n\n".join(parts)
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={
                "title": title,
                "reply_count": thread.get("reply_count", 0),
                "post_pages": page,
            },
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """XenForo permissions are role-based; return empty."""
        return []

    async def health_check(self) -> bool:
        """Verify connectivity by fetching the forum index."""
        if self._client is None:
            return False
        try:
            data = await self._get_json("/api/index")
            return isinstance(data, dict)
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()