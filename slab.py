"""Slab connector.

API: Slab GraphQL API v1
Auth: Bearer token
Sync: Full (Slab GraphQL has limited filtering)
Permissions: Not doc-level, returns empty

Content types indexed:
  - Posts (markdown content)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

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

SLAB_GRAPHQL_URL = "https://api.slab.com/v1/graphql"


class SlabConnector(ConnectorBase):
    """Slab knowledge-base connector using GraphQL API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Hold onto a possible config-side token so authenticate()
        # can fall back when the sync route hasn't materialised it
        # into credentials. API-key Slab sources created via
        # AddKnowledgeModal carry the token in config under `slabToken`.
        self._fallback_token: str = config.get("slabToken") or ""
        self._client: RetryClient | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using a Bearer token.

        Expects credentials: {access_token: str}
        """
        # Prefer canonical credentials path. Fall back to the token
        # captured from config in __init__ — that's how API-key Slab
        # sources created via AddKnowledgeModal carry the token today.
        token = credentials.get("access_token") or self._fallback_token or ""
        if not token:
            raise ConnectorAuthError(
                "Slab requires access_token",
                connector_type="slab",
            )

        self._client = RetryClient(
            base_url="",
            headers={
                **bearer_headers(token),
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity with a lightweight query
        try:
            result = await self._graphql(
                "{ organization { name } }"
            )
            org_name = result.get("organization", {}).get("name", "unknown")
            logger.info("Slab authenticated for organization: %s", org_name)
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"Slab authentication failed: {e}",
                connector_type="slab",
            ) from e

    # ------------------------------------------------------------------
    # GraphQL helper
    # ------------------------------------------------------------------

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query against the Slab API."""
        assert self._client is not None

        body: dict = {"query": query}
        if variables:
            body["variables"] = variables

        try:
            resp = await self._client.post(SLAB_GRAPHQL_URL, json=body)
        except Exception as e:
            _raise_typed(e, "slab")

        data = resp.json()
        errors = data.get("errors")
        if errors:
            msg = errors[0].get("message", str(errors))
            logger.warning("Slab GraphQL error: %s", msg)
            raise ConnectorTransientError(
                f"Slab GraphQL error: {msg}",
                connector_type="slab",
            )

        return data.get("data", {})

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all posts via GraphQL.

        Slab's GraphQL API returns all posts; we filter client-side by updatedAt.
        """
        query = """
        query ListPosts($after: String) {
            posts(after: $after) {
                nodes {
                    id
                    title
                    updatedAt
                    insertedAt
                }
                pageInfo {
                    hasNextPage
                    endCursor
                }
            }
        }
        """

        cursor: str | None = None
        while True:
            variables: dict = {}
            if cursor:
                variables["after"] = cursor

            data = await self._graphql(query, variables)
            posts_data = data.get("posts", {})
            nodes = posts_data.get("nodes", [])

            if not nodes:
                break

            for post in nodes:
                updated_at = _parse_dt(post.get("updatedAt", ""))
                if since and updated_at <= since:
                    continue

                post_id = post["id"]
                yield DocumentMetadata(
                    external_id=post_id,
                    title=post.get("title", "Untitled"),
                    url=None,  # Slab doesn't return URLs in GraphQL
                    content_type="text/markdown",
                    modified_at=updated_at,
                    metadata={},
                )

            page_info = posts_data.get("pageInfo", {})
            if not page_info.get("hasNextPage", False):
                break
            cursor = page_info.get("endCursor")

    # ------------------------------------------------------------------
    # Fetch document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single post's markdown content via GraphQL."""
        query = """
        query GetPost($id: ID!) {
            post(id: $id) {
                id
                title
                content
            }
        }
        """
        data = await self._graphql(query, {"id": doc_id})
        post = data.get("post", {})

        if not post:
            raise ConnectorTransientError(
                f"Slab post {doc_id} not found",
                connector_type="slab",
            )

        content = post.get("content", "")
        title = post.get("title", "Untitled")

        # Prepend title as markdown heading
        markdown = f"# {title}\n\n{content}"

        return RawDocument(
            external_id=doc_id,
            content=markdown.encode("utf-8"),
            content_type="text/markdown",
            metadata={"title": title},
        )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Slab does not expose doc-level permissions via API."""
        return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._graphql("{ organization { name } }")
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