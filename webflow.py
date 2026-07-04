"""Webflow connector.

API: Webflow Data API v2
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (lastUpdated sort)
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
_BASE = "https://api.webflow.com/v2"


class WebflowConnector(ConnectorBase):
    """Native Webflow connector via Data API v2."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._site_ids: list[str] = config.get("site_ids", [])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Webflow requires 'access_token'", connector_type="webflow")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/token/authorized_by")
            resp.json()
            logger.info("Webflow authenticated")
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Webflow auth failed: {exc}", connector_type="webflow") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        site_ids = list(self._site_ids)
        if not site_ids:
            try:
                resp = await self._client.get("/sites")
                for site in resp.json().get("sites", []):
                    site_ids.append(site["id"])
            except Exception as exc:
                _raise_mapped(exc, "webflow")
                raise
        for site_id in site_ids:
            try:
                resp = await self._client.get(f"/sites/{site_id}/collections")
            except Exception as exc:
                _raise_mapped(exc, "webflow")
                raise
            for collection in resp.json().get("collections", []):
                offset = 0
                while True:
                    try:
                        items_resp = await self._client.get(
                            f"/collections/{collection['id']}/items",
                            params={"offset": offset, "limit": 100},
                        )
                    except Exception as exc:
                        _raise_mapped(exc, "webflow")
                        raise
                    body = items_resp.json()
                    items = body.get("items", [])
                    for item in items:
                        modified = _parse_ts(item.get("lastUpdated", item.get("updatedOn", "")))
                        if since and modified < since:
                            continue
                        name = item.get("fieldData", {}).get("name") or item.get("fieldData", {}).get("title") or item.get("id", "")
                        yield DocumentMetadata(
                            external_id=f"{collection['id']}:{item['id']}",
                            title=name,
                            content_type="text/html",
                            modified_at=modified,
                            folder_id=collection["id"],
                            metadata={"collection": collection.get("displayName", collection.get("slug", "")), "site_id": site_id, "item_id": item["id"]},
                        )
                    if len(items) < 100:
                        break
                    offset += 100

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        # doc_id is a compound key: "collection_id:item_id"
        if ":" in doc_id:
            collection_id, item_id = doc_id.split(":", 1)
        else:
            collection_id, item_id = "", doc_id
        try:
            resp = await self._client.get(f"/collections/{collection_id}/items/{item_id}")
        except Exception as exc:
            _raise_mapped(exc, "webflow")
            raise
        item = resp.json()
        field_data = item.get("fieldData", {})
        name = field_data.get("name") or field_data.get("title") or doc_id
        parts = [f"# {name}", ""]
        for key, val in field_data.items():
            if val and isinstance(val, str) and len(val) < 5000:
                parts.append(f"**{key}:** {val}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/html", metadata={"title": name})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/sites")
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
