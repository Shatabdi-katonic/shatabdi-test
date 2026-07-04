"""Document360 connector.

API: Document360 REST API v2
Auth: API key via ``api_token`` header
Sync: Incremental (modified_at filter) + full
Permissions: Not doc-level, returns empty

Content types indexed:
  - Articles (HTML content)
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

D360_API = "https://apihub.document360.io"
PAGE_SIZE = 200


class Document360Connector(ConnectorBase):
    """Document360 knowledge-base connector for articles."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept canonical key (`project_version_id`) and frontend
        # form key (`doc360ProjectId`). AddKnowledgeModal sends
        # `doc360ProjectId` directly into config; without this
        # fallback the optional project-version filter from the UI
        # is silently dropped.
        self._project_version_id: str = (
            config.get("project_version_id")
            or config.get("doc360ProjectId")
            or ""
        )
        # Hold onto a possible config-side API key so authenticate()
        # can fall back when the sync route hasn't materialised it
        # into credentials. API-key Document360 sources created via
        # AddKnowledgeModal carry the key in config under `doc360ApiKey`.
        self._fallback_api_key: str = config.get("doc360ApiKey") or ""
        self._client: RetryClient | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with Document360 API key.

        Expects credentials: {api_key: str}
        """
        # Prefer canonical credentials path. Fall back to the key
        # captured from config in __init__ — that's how API-key
        # Document360 sources created via AddKnowledgeModal carry
        # the key today.
        api_key = credentials.get("api_key") or self._fallback_api_key or ""
        if not api_key:
            raise ConnectorAuthError(
                "Document360 requires api_key",
                connector_type="document360",
            )

        self._client = RetryClient(
            base_url=D360_API,
            headers={
                "api_token": api_key,
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity by fetching project info
        try:
            data = await self._client.get_json("/v2/ProjectVersions")
            versions = data.get("data", [])
            if versions and not self._project_version_id:
                self._project_version_id = versions[0].get("id", "")
            logger.info(
                "Document360 authenticated, project_version=%s",
                self._project_version_id,
            )
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"Document360 authentication failed: {e}",
                connector_type="document360",
            ) from e

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all articles, optionally filtering by modified_at > since."""
        assert self._client is not None
        page = 1

        while True:
            params: dict = {"page": page, "limit": PAGE_SIZE}
            if self._project_version_id:
                params["project_version_id"] = self._project_version_id

            try:
                data = await self._client.get_json("/v2/articles", params=params)
            except Exception as e:
                _raise_typed(e, "document360")

            articles = data.get("data", [])
            if not articles:
                break

            for article in articles:
                modified_at = _parse_dt(article.get("modified_at", ""))
                if since and modified_at <= since:
                    continue

                yield DocumentMetadata(
                    external_id=str(article["id"]),
                    title=article.get("title", "Untitled"),
                    url=article.get("url") or article.get("slug"),
                    content_type="text/html",
                    author=article.get("author", {}).get("name"),
                    modified_at=modified_at,
                    metadata={
                        "category_id": article.get("category_id"),
                        "status": article.get("status"),
                    },
                )

            # Check if we've fetched all articles
            if len(articles) < PAGE_SIZE:
                break
            page += 1

    # ------------------------------------------------------------------
    # Fetch document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch article by ID. Returns HTML content."""
        assert self._client is not None

        params: dict = {}
        if self._project_version_id:
            params["project_version_id"] = self._project_version_id

        try:
            data = await self._client.get_json(f"/v2/articles/{doc_id}", params=params)
        except Exception as e:
            _raise_typed(e, "document360")

        article = data.get("data", {})
        html = article.get("html_content", "") or article.get("content", "")
        title = article.get("title", "Untitled")

        return RawDocument(
            external_id=doc_id,
            content=html.encode("utf-8"),
            content_type="text/html",
            metadata={"title": title},
        )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Document360 does not expose doc-level permissions."""
        return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/v2/ProjectVersions")
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
    """Parse an ISO datetime string from Document360."""
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