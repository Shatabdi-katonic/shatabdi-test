"""OneDrive connector.

API: Microsoft Graph API v1.0
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (delta API)
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
_BASE = "https://graph.microsoft.com/v1.0"


class OneDriveConnector(ConnectorBase):
    """Native OneDrive connector via Microsoft Graph API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._folder_path: str = config.get("folder_path", "")
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("OneDrive requires 'access_token'", connector_type="onedrive")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/me")
            me = resp.json()
            logger.info("OneDrive authenticated as %s", me.get("displayName", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"OneDrive auth failed: {exc}", connector_type="onedrive") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        if self._folder_path:
            endpoint = f"/me/drive/root:/{self._folder_path}:/children"
        else:
            endpoint = "/me/drive/root/children"
        next_link: str | None = endpoint
        while next_link:
            try:
                if next_link.startswith("http"):
                    resp = await self._client.get(next_link.replace(_BASE, ""))
                else:
                    resp = await self._client.get(next_link)
            except Exception as exc:
                _raise_mapped(exc, "onedrive")
                raise
            body = resp.json()
            for item in body.get("value", []):
                if "folder" in item:
                    continue
                modified = _parse_ts(item.get("lastModifiedDateTime", ""))
                if since and modified < since:
                    continue
                yield DocumentMetadata(
                    external_id=item["id"],
                    title=item.get("name", ""),
                    url=item.get("webUrl"),
                    content_type=item.get("file", {}).get("mimeType", "application/octet-stream"),
                    author=((item.get("lastModifiedBy") or {}).get("user") or {}).get("email"),
                    modified_at=modified,
                    metadata={"size": item.get("size")},
                )
            next_link = body.get("@odata.nextLink")

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/me/drive/items/{doc_id}")
        except Exception as exc:
            _raise_mapped(exc, "onedrive")
            raise
        meta = resp.json()
        try:
            content_resp = await self._client.get(f"/me/drive/items/{doc_id}/content")
            file_bytes = content_resp.content
        except Exception:
            file_bytes = b""
        return RawDocument(
            external_id=doc_id,
            content=file_bytes,
            content_type=meta.get("file", {}).get("mimeType", "application/octet-stream"),
            metadata={"title": meta.get("name", ""), "size": meta.get("size", 0)},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/me/drive")
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
