"""Box connector.

API: Box Content API v2
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (content_modified_at filter via search)
Permissions: Not supported
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers
from platform_knowledge_engine.connectors.base import (
    ConnectorAuthError, ConnectorBase, ConnectorRateLimitError,
    ConnectorTransientError, DocumentMetadata, PermissionEntry, RawDocument,
)

logger = logging.getLogger(__name__)
_BASE = "https://api.box.com/2.0"


def _content_type_from_name(filename: str) -> str:
    """Guess a MIME type from a filename's extension.

    Box's file listing/info only gives us the file name, so the ingestion
    pipeline relies on this to route the parser. This helper was referenced
    at two call sites but never defined — every Box sync raised
    ``NameError: name '_content_type_from_name' is not defined`` at discovery,
    which surfaced as ``Connector error`` / sync_status=error. Falls back to
    ``application/octet-stream`` so the parser still receives a usable type.
    """
    mime, _ = mimetypes.guess_type(filename or "")
    return mime or "application/octet-stream"


class BoxConnector(ConnectorBase):
    """Native Box connector via Content API v2."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._folder_ids: list[str] = config.get("folder_ids", ["0"])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Box requires 'access_token'", connector_type="box")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/users/me")
            me = resp.json()
            logger.info("Box authenticated as %s", me.get("name", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Box auth failed: {exc}", connector_type="box") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        # Use a stack for recursive folder traversal
        folder_stack = list(self._folder_ids)
        while folder_stack:
            folder_id = folder_stack.pop()
            offset = 0
            while True:
                try:
                    resp = await self._client.get(
                        f"/folders/{folder_id}/items",
                        params={"fields": "id,name,type,modified_at,size,created_by", "limit": 1000, "offset": offset},
                    )
                except Exception as exc:
                    _raise_mapped(exc, "box")
                    raise
                body = resp.json()
                for entry in body.get("entries", []):
                    entry_type = entry.get("type")
                    # Recurse into subfolders
                    if entry_type == "folder":
                        folder_stack.append(entry["id"])
                        continue
                    if entry_type != "file":
                        continue
                    modified = _parse_ts(entry.get("modified_at", ""))
                    if since and modified < since:
                        continue
                    filename = entry.get("name", "")
                    yield DocumentMetadata(
                        external_id=entry["id"],
                        title=filename,
                        content_type=_content_type_from_name(filename),
                        modified_at=modified,
                        folder_id=folder_id,
                        metadata={"size": entry.get("size")},
                    )
                if offset + body.get("limit", 1000) >= body.get("total_count", 0):
                    break
                offset += body.get("limit", 1000)

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/files/{doc_id}", params={"fields": "id,name,description,modified_at,size,content_modified_at"})
        except Exception as exc:
            _raise_mapped(exc, "box")
            raise
        meta = resp.json()
        filename = meta.get("name", "")

        # Download file content.
        # Box /files/{id}/content returns 302 → dl.boxcloud.com.
        # httpx strips the Authorization header on cross-domain redirects
        # (security feature), but the Box CDN redirect URL includes its own
        # auth token in the query string. We must handle the redirect manually
        # to avoid 403 errors.
        import httpx as _httpx

        file_bytes = None
        try:
            # Step 1: Request content with redirects DISABLED to get the CDN URL
            raw_client = self._client._client  # access underlying httpx.AsyncClient
            content_resp = await raw_client.request(
                "GET",
                f"/files/{doc_id}/content",
                follow_redirects=False,
            )

            if content_resp.status_code in (301, 302, 307):
                # Step 2: Follow the redirect to CDN — no auth header needed
                cdn_url = content_resp.headers.get("location", "")
                if cdn_url:
                    async with _httpx.AsyncClient(timeout=60.0) as cdn_client:
                        cdn_resp = await cdn_client.get(cdn_url)
                        cdn_resp.raise_for_status()
                        file_bytes = cdn_resp.content
            elif content_resp.status_code == 200:
                # Some Box endpoints return content directly (no redirect)
                file_bytes = content_resp.content
            else:
                content_resp.raise_for_status()
        except Exception as first_exc:
            # Fallback: try with follow_redirects=True (works for some token types)
            logger.warning("Box content download (no-redirect) failed for %s: %s", doc_id, first_exc)
            try:
                content_resp = await self._client.get(f"/files/{doc_id}/content")
                file_bytes = content_resp.content
            except Exception:
                pass

            if not file_bytes:
                # Final fallback: build text from file metadata
                desc = meta.get("description", "")
                if desc:
                    file_bytes = f"# {filename}\n\n{desc}".encode("utf-8")
                else:
                    raise ConnectorTransientError(
                        f"Failed to download file content for {filename} ({doc_id}): {first_exc}",
                        connector_type="box",
                    ) from first_exc

        if not file_bytes:
            raise ConnectorTransientError(
                f"Empty file content for {filename} ({doc_id}) — Box returned 0 bytes",
                connector_type="box",
            )

        # Infer content type from filename extension
        ct = _content_type_from_name(filename)

        return RawDocument(
            external_id=doc_id,
            content=file_bytes,
            content_type="application/octet-stream",
            metadata={"title": meta.get("name", ""), "size": meta.get("size", 0)},
        )

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
