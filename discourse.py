"""Discourse connector.

API: Discourse REST API
Auth: API key + API username via request headers
Sync: Incremental (bumped_at filter) + full
Permissions: Category-level (not doc-level), returns empty

Content types indexed:
  - Topics with their posts rendered as HTML
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import RetryClient
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


class DiscourseConnector(ConnectorBase):
    """Discourse forum connector for topics and posts."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept canonical key (`base_url`) and frontend form key
        # (`discourseUrl`). AddKnowledgeModal sends `discourseUrl`
        # directly into config; without this fallback the connector
        # falls back to credentials.base_url (which is empty for
        # API-key sources), and the sync silently lists 0 documents.
        self._base_url: str = (
            config.get("base_url") or config.get("discourseUrl") or ""
        ).rstrip("/")
        # Capture optional category filter from the frontend form
        # (`discourseCategories` is a comma-separated string). When
        # set, list_documents() can filter to these categories only.
        self._category_filter: set[str] = self._parse_csv(
            config.get("discourseCategories")
        )
        # Hold onto a possible config-side API key so authenticate()
        # can fall back when the sync route hasn't materialised it
        # into credentials.
        self._fallback_api_key: str = config.get("discourseApiKey") or ""
        self._client: RetryClient | None = None

    @staticmethod
    def _parse_csv(value: str | None) -> set[str]:
        """Parse comma-separated values into a set; empty -> no filter."""
        if not value or not isinstance(value, str):
            return set()
        return {c.strip() for c in value.split(",") if c.strip()}

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using api_key and api_username.

        Expects credentials: {api_key: str, api_username: str, base_url?: str}
        """
        # Prefer canonical credentials path. Fall back to the key
        # captured from config in __init__ — that's how API-key
        # Discourse sources created via AddKnowledgeModal carry the
        # API key today.
        api_key = credentials.get("api_key") or self._fallback_api_key or ""
        api_username = credentials.get("api_username", "system")
        if not api_key:
            raise ConnectorAuthError(
                "Discourse requires api_key",
                connector_type="discourse",
            )

        if not self._base_url:
            base_url = credentials.get("base_url", "")
            if not base_url:
                raise ConnectorAuthError(
                    "Discourse requires base_url in config or credentials",
                    connector_type="discourse",
                )
            self._base_url = base_url.rstrip("/")

        self._client = RetryClient(
            base_url=self._base_url,
            headers={
                "Api-Key": api_key,
                "Api-Username": api_username,
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity
        try:
            await self._client.get_json("/about.json")
            logger.info("Discourse authenticated at %s", self._base_url)
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"Discourse authentication failed: {e}",
                connector_type="discourse",
            ) from e

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List topics from /latest.json, paginated by page number.

        Filters by bumped_at > since when doing incremental sync.
        """
        assert self._client is not None
        page = 0

        while True:
            try:
                data = await self._client.get_json(
                    "/latest.json", params={"page": page}
                )
            except Exception as e:
                _raise_typed(e, "discourse")

            topic_list = data.get("topic_list", {})
            topics = topic_list.get("topics", [])
            if not topics:
                break

            found_old = False
            for topic in topics:
                bumped_at = _parse_dt(topic.get("bumped_at", ""))

                if since and bumped_at <= since:
                    found_old = True
                    continue

                topic_id = str(topic["id"])
                yield DocumentMetadata(
                    external_id=topic_id,
                    title=topic.get("title", "Untitled"),
                    url=f"{self._base_url}/t/{topic.get('slug', '')}/{topic_id}",
                    content_type="text/html",
                    author=topic.get("last_poster_username"),
                    modified_at=bumped_at,
                    metadata={
                        "category_id": topic.get("category_id"),
                        "posts_count": topic.get("posts_count", 0),
                        "views": topic.get("views", 0),
                    },
                )

            # If all remaining topics are older than since, stop
            if found_old and since:
                break

            # Check for more pages
            more_url = topic_list.get("more_topics_url")
            if not more_url:
                break
            page += 1

    # ------------------------------------------------------------------
    # Fetch document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a topic with all its posts as combined HTML."""
        assert self._client is not None

        try:
            data = await self._client.get_json(f"/t/{doc_id}.json")
        except Exception as e:
            _raise_typed(e, "discourse")

        title = data.get("title", "Untitled")
        post_stream = data.get("post_stream", {})
        posts = post_stream.get("posts", [])

        # Combine all posts into a single HTML document
        html_parts = [f"<h1>{_escape_html(title)}</h1>"]
        for post in posts:
            username = post.get("username", "unknown")
            cooked = post.get("cooked", "")  # Discourse pre-renders HTML
            created = post.get("created_at", "")
            html_parts.append(
                f'<div class="post" data-user="{_escape_html(username)}" '
                f'data-date="{_escape_html(created)}">'
                f"<strong>{_escape_html(username)}</strong>: {cooked}</div>"
            )

        html = "\n".join(html_parts)
        return RawDocument(
            external_id=doc_id,
            content=html.encode("utf-8"),
            content_type="text/html",
            metadata={"title": title, "posts_count": len(posts)},
        )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Discourse uses category-level permissions, not doc-level."""
        return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/about.json")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_dt(s: str) -> datetime:
    """Parse an ISO datetime string from Discourse."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _escape_html(s: str) -> str:
    """Minimal HTML escaping for attribute/text embedding."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


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