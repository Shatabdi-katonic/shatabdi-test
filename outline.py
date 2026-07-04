"""Outline connector.

API: Outline REST API (POST-based)
Auth: Bearer token
Sync: Incremental (dateFilter) + full
Permissions: Document-level memberships

Content types indexed:
  - Documents (markdown content)
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

PAGE_LIMIT = 100


class OutlineConnector(ConnectorBase):
    """Outline wiki connector for documents."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept canonical key (`base_url`) and frontend form key (`outlineUrl`).
        # The AddKnowledgeModal sends `outlineUrl` directly into config; without
        # this fallback the connector loses the user-entered URL and falls back
        # to credentials.base_url (which is empty for API-key sources), and the
        # sync silently lists 0 documents.
        self._base_url: str = self._ensure_scheme(
            config.get("base_url") or config.get("outlineUrl") or ""
        )
        # Same story for the collection filter — frontend ships
        # `outlineCollections` as a comma-separated string; back-compat with
        # the older `collection_id` field stays intact.
        self._collection_id: str | None = (
            config.get("collection_id")
            or self._parse_first_collection(config.get("outlineCollections"))
        )
        # Hold onto a possible config-side token so authenticate() can fall
        # back when the sync route hasn't materialised one into credentials.
        self._fallback_token: str = config.get("outlineToken") or ""
        self._client: RetryClient | None = None

    @staticmethod
    def _parse_first_collection(value: str | None) -> str | None:
        """Return the first non-empty comma-separated entry, or None."""
        if not value or not isinstance(value, str):
            return None
        first = value.split(",")[0].strip()
        return first or None

    @staticmethod
    def _ensure_scheme(url: str) -> str:
        """Normalise the Outline base URL to carry an http(s):// scheme.

        The AddKnowledgeModal "Outline URL" field commonly gets a bare host
        pasted in (e.g. ``mycompany.getoutline.com``). httpx rejects a
        schemeless base_url with "Request URL is missing an 'http://' or
        'https://' protocol" — which surfaced as ``Outline authentication
        failed`` and a 0-document sync (status flipped to ``error`` once the
        CR-541 masking fix landed). Default to https:// when no scheme is
        present; leave a correctly-schemed URL untouched.
        """
        url = (url or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using a Bearer token.

        Expects credentials: {access_token: str, base_url?: str}
        For API-key sources created via AddKnowledgeModal the token arrives
        in config under `outlineToken`; __init__ captures it as
        `_fallback_token` and this method uses it when credentials don't
        carry an access_token.
        """
        # Prefer the canonical credentials path (OAuth callback writes
        # `access_token` here). Fall back to the token captured from config
        # in __init__ — that's how API-key sources created via
        # AddKnowledgeModal carry the token today.
        token = credentials.get("access_token") or self._fallback_token or ""
        if not token:
            raise ConnectorAuthError(
                "Outline requires access_token",
                connector_type="outline",
            )

        if not self._base_url:
            base_url = credentials.get("base_url", "")
            if not base_url:
                raise ConnectorAuthError(
                    "Outline requires base_url in config or credentials",
                    connector_type="outline",
                )
            self._base_url = self._ensure_scheme(base_url)

        self._client = RetryClient(
            base_url=self._base_url,
            headers={
                **bearer_headers(token),
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity
        try:
            resp = await self._client.post("/api/auth.info", json={})
            data = resp.json()
            team = data.get("data", {}).get("team", {}).get("name", "unknown")
            logger.info("Outline authenticated for team: %s", team)
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"Outline authentication failed: {e}",
                connector_type="outline",
            ) from e

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List documents via POST /api/documents.list with offset pagination."""
        assert self._client is not None
        offset = 0
        yielded = 0

        # Surface the active collection scope up-front. A non-empty filter that
        # is NOT a valid Outline collection UUID (e.g. a user typed a collection
        # *name* into the "Collections" field) makes Outline return HTTP 200
        # with an empty `data` array — the sync then completes "successfully"
        # with 0 documents. Logging it makes that otherwise-invisible cause
        # diagnosable from the connector logs.
        if self._collection_id:
            logger.info(
                "Outline list_documents scoped to collectionId=%s (only the "
                "first of outlineCollections is applied)",
                self._collection_id,
            )

        while True:
            body: dict = {
                "offset": offset,
                "limit": PAGE_LIMIT,
                "sort": "updatedAt",
                # Outline coerces any value other than "ASC" to "DESC"; send the
                # canonical uppercase token to match the documented API contract.
                "direction": "DESC",
            }
            if self._collection_id:
                body["collectionId"] = self._collection_id
            if since:
                # Calculate appropriate dateFilter based on how far back `since` is
                from datetime import UTC, datetime as dt
                delta = dt.now(UTC) - since
                if delta.days <= 1:
                    body["dateFilter"] = "day"
                elif delta.days <= 7:
                    body["dateFilter"] = "week"
                elif delta.days <= 30:
                    body["dateFilter"] = "month"
                else:
                    body["dateFilter"] = "year"

            try:
                resp = await self._client.post("/api/documents.list", json=body)
                data = resp.json()
            except Exception as e:
                _raise_typed(e, "outline")

            docs = data.get("data", [])
            if not docs:
                break

            for doc in docs:
                updated_at = _parse_dt(doc.get("updatedAt", ""))
                if since and updated_at <= since:
                    # Sorted desc, safe to stop
                    return

                yielded += 1
                yield DocumentMetadata(
                    external_id=doc["id"],
                    title=doc.get("title", "Untitled"),
                    url=f"{self._base_url}{doc.get('url', '')}",
                    content_type="text/markdown",
                    author=doc.get("createdBy", {}).get("name"),
                    modified_at=updated_at,
                    metadata={
                        "collection_id": doc.get("collectionId"),
                        "parent_document_id": doc.get("parentDocumentId"),
                        "revision": doc.get("revision", 0),
                    },
                )

            pagination = data.get("pagination", {})
            total = pagination.get("total", 0)
            offset += PAGE_LIMIT
            if offset >= total:
                break

        if yielded == 0:
            logger.warning(
                "Outline list_documents returned 0 documents "
                "(base_url=%s, collection_filter=%s, since=%s). If the workspace "
                "has documents, verify the API token's user can access them and "
                "that any collection filter is a valid collection UUID.",
                self._base_url,
                self._collection_id,
                since.isoformat() if since else None,
            )

    # ------------------------------------------------------------------
    # Fetch document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch document markdown via POST /api/documents.info."""
        assert self._client is not None

        try:
            resp = await self._client.post("/api/documents.info", json={"id": doc_id})
            data = resp.json()
        except Exception as e:
            _raise_typed(e, "outline")

        doc = data.get("data", {})
        text = doc.get("text", "")
        title = doc.get("title", "Untitled")

        return RawDocument(
            external_id=doc_id,
            content=text.encode("utf-8"),
            content_type="text/markdown",
            metadata={"title": title},
        )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Fetch document-level memberships from Outline."""
        assert self._client is not None
        entries: list[PermissionEntry] = []

        try:
            offset = 0
            while True:
                resp = await self._client.post(
                    "/api/documents.memberships",
                    json={"id": doc_id, "offset": offset, "limit": PAGE_LIMIT},
                )
                data = resp.json()
                memberships = data.get("data", [])

                if not memberships:
                    break

                for m in memberships:
                    user = m.get("user", {})
                    permission = m.get("permission", "read")

                    relation = "viewer" if permission in ("read", "read_write") else "editor"
                    if permission == "admin":
                        relation = "owner"

                    user_email = user.get("email", "")
                    user_id = user_email or user.get("id", "")
                    if user_id:
                        entries.append(
                            PermissionEntry(
                                subject_type="user",
                                subject_id=user_id,
                                relation=relation,
                            )
                        )

                pagination = data.get("pagination", {})
                total = pagination.get("total", 0)
                offset += PAGE_LIMIT
                if offset >= total:
                    break

        except Exception as e:
            logger.warning("Failed to get permissions for %s: %s", doc_id, e)

        return entries

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.post("/api/auth.info", json={})
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