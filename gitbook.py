"""GitBook connector.

API: GitBook REST API v1
Auth: Bearer token
Sync: Full (GitBook API has limited incremental support)
Permissions: Not doc-level, returns empty

Content types indexed:
  - Pages (markdown content) within GitBook spaces
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

GITBOOK_API = "https://api.gitbook.com"


class GitBookConnector(ConnectorBase):
    """GitBook connector for space pages."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept canonical key (`space_id`) and frontend form key
        # (`gitbookSpaceId`). AddKnowledgeModal sends `gitbookSpaceId`
        # directly into config; without this fallback the optional space
        # filter from the UI is silently dropped (same field-naming
        # mismatch pattern as the Outline and Discord connectors).
        self._space_id: str = (
            config.get("space_id") or config.get("gitbookSpaceId") or ""
        )
        # Hold onto a possible config-side token so authenticate() can
        # fall back when the sync route hasn't materialised one into
        # credentials. API-key GitBook sources created via the UI carry
        # the token in config under `gitbookToken`.
        self._fallback_token: str = config.get("gitbookToken") or ""
        self._client: RetryClient | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using a Bearer token.

        Expects credentials: {access_token: str}
        For API-key sources created via AddKnowledgeModal the token
        arrives in config under `gitbookToken`; __init__ captures it as
        `_fallback_token` and this method uses it when credentials don't
        carry an access_token.
        """
        # Prefer the canonical credentials path. Fall back to the token
        # captured from config in __init__ — that's how API-key GitBook
        # sources created via AddKnowledgeModal carry the token today.
        token = credentials.get("access_token") or self._fallback_token or ""
        if not token:
            raise ConnectorAuthError(
                "GitBook requires access_token",
                connector_type="gitbook",
            )

        self._client = RetryClient(
            base_url=GITBOOK_API,
            headers={
                **bearer_headers(token),
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity
        try:
            await self._client.get_json("/v1/user")
            logger.info("GitBook authenticated")
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"GitBook authentication failed: {e}",
                connector_type="gitbook",
            ) from e

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all pages across configured space(s).

        If no space_id is configured, discovers all accessible spaces first.
        """
        assert self._client is not None

        space_ids = await self._resolve_spaces()

        for space_id in space_ids:
            async for doc in self._list_space_pages(space_id, since):
                yield doc

    async def _resolve_spaces(self) -> list[str]:
        """Return list of space IDs to index."""
        if self._space_id:
            return [self._space_id]

        assert self._client is not None
        # CR-620: GitBook has NO top-level /v1/spaces endpoint (returns 404).
        # Spaces are nested under organizations: list /v1/orgs, then for each org
        # list /v1/orgs/{orgId}/spaces. The old /v1/spaces call 404'd, so a source
        # without an explicit Space ID could never discover anything.
        spaces: list[str] = []

        # 1) organizations the token can see
        org_ids: list[str] = []
        page: str | None = None
        while True:
            params: dict = {"page": page} if page else {}
            try:
                data = await self._client.get_json("/v1/orgs", params=params)
            except Exception as e:
                _raise_typed(e, "gitbook")
            for org in data.get("items", []):
                if org.get("id"):
                    org_ids.append(org["id"])
            page = (data.get("next") or {}).get("page")
            if not page:
                break

        # 2) spaces within each organization
        for org_id in org_ids:
            page = None
            while True:
                params = {"page": page} if page else {}
                try:
                    data = await self._client.get_json(
                        f"/v1/orgs/{org_id}/spaces", params=params
                    )
                except Exception as e:
                    _raise_typed(e, "gitbook")
                for space in data.get("items", []):
                    if space.get("id"):
                        spaces.append(space["id"])
                page = (data.get("next") or {}).get("page")
                if not page:
                    break

        logger.info(
            "GitBook discovered %d spaces across %d org(s)", len(spaces), len(org_ids)
        )
        return spaces

    async def _list_space_pages(
        self, space_id: str, since: datetime | None
    ) -> AsyncIterator[DocumentMetadata]:
        """List pages within a single space by walking the content tree."""
        assert self._client is not None

        try:
            data = await self._client.get_json(f"/v1/spaces/{space_id}/content")
        except Exception as e:
            _raise_typed(e, "gitbook")

        pages = data.get("pages", [])
        # Flatten the tree
        stack = list(pages)
        while stack:
            node = stack.pop()
            node_type = node.get("type", "")
            if node_type == "group":
                # Groups contain nested pages
                stack.extend(node.get("pages", []))
                continue

            page_id = node.get("id", "")
            if not page_id:
                continue

            updated_at = _parse_dt(node.get("updatedAt", ""))
            if since and updated_at <= since:
                continue

            title = node.get("title", "") or node.get("path", "Untitled")
            yield DocumentMetadata(
                external_id=f"{space_id}:{page_id}",
                title=title,
                url=node.get("url"),
                content_type="text/markdown",
                modified_at=updated_at,
                metadata={"space_id": space_id, "path": node.get("path", "")},
            )

            # Recurse into nested pages
            stack.extend(node.get("pages", []))

    # ------------------------------------------------------------------
    # Fetch document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a page's markdown content.

        doc_id format: ``{space_id}:{page_id}``
        """
        assert self._client is not None

        space_id, page_id = doc_id.split(":", 1)

        try:
            data = await self._client.get_json(
                f"/v1/spaces/{space_id}/content/page/{page_id}"
            )
        except Exception as e:
            _raise_typed(e, "gitbook")

        # GitBook returns markdown in the "markdown" field, or document nodes
        markdown = data.get("markdown", "")
        if not markdown:
            # Fallback: render document nodes
            title = data.get("title", "Untitled")
            description = data.get("description", "")
            markdown = f"# {title}\n\n{description}"

        return RawDocument(
            external_id=doc_id,
            content=markdown.encode("utf-8"),
            content_type="text/markdown",
            metadata={"title": data.get("title", "")},
        )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """GitBook does not expose doc-level permissions via API."""
        return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/v1/user")
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