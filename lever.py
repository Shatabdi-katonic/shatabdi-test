"""Lever connector.

API: Lever REST API v1
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (updated_at_start filter)
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
_BASE = "https://api.lever.co/v1"


class LeverConnector(ConnectorBase):
    """Native Lever connector via REST API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._content_types: list[str] = config.get("content_types", ["opportunities", "postings"])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Lever requires 'access_token'", connector_type="lever")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/postings", params={"limit": 1})
            resp.json()
            logger.info("Lever authenticated")
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Lever auth failed: {exc}", connector_type="lever") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        for entity_type in self._content_types:
            offset: str | None = None
            while True:
                params: dict = {"limit": 100}
                if offset:
                    params["offset"] = offset
                if since:
                    params["updated_at_start"] = str(int(since.timestamp() * 1000))
                try:
                    resp = await self._client.get(f"/{entity_type}", params=params)
                except Exception as exc:
                    _raise_mapped(exc, "lever")
                    raise
                body = resp.json()
                items = body.get("data", [])
                for item in items:
                    name = item.get("name") or item.get("text") or f"{entity_type} {item.get('id', '')}"
                    ts_ms = item.get("updatedAt") or item.get("createdAt") or 0
                    modified = datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else datetime.now(UTC)
                    yield DocumentMetadata(
                        external_id=f"{entity_type}_{item['id']}",
                        title=name,
                        url=item.get("urls", {}).get("show") if isinstance(item.get("urls"), dict) else None,
                        content_type="text/plain",
                        modified_at=modified,
                        metadata={"type": entity_type},
                    )
                if not body.get("hasNext"):
                    break
                offset = body.get("next")

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        parts_split = doc_id.split("_", 1)
        entity_type = parts_split[0] if len(parts_split) == 2 else "opportunities"
        entity_id = parts_split[1] if len(parts_split) == 2 else doc_id
        try:
            resp = await self._client.get(f"/{entity_type}/{entity_id}")
        except Exception as exc:
            _raise_mapped(exc, "lever")
            raise
        item = resp.json().get("data", {})
        name = item.get("name") or item.get("text") or doc_id
        parts = [f"# {name}", ""]
        if entity_type == "opportunities":
            contact = item.get("contact") or item.get("name", "")
            if contact:
                parts.append(f"**Contact:** {contact}")
            stage = (item.get("stage") or {})
            if isinstance(stage, dict):
                parts.append(f"**Stage:** {stage.get('text', '')}")
            for key in ["headline", "location", "origin", "sources"]:
                val = item.get(key)
                if val:
                    if isinstance(val, list):
                        val = ", ".join(str(v) for v in val)
                    parts.append(f"**{key.title()}:** {val}")
        elif entity_type == "postings":
            for key in ["state", "team", "department", "location", "workplaceType"]:
                val = item.get(key)
                if val:
                    parts.append(f"**{key.title()}:** {val}")
            if item.get("content"):
                content_data = item["content"]
                if isinstance(content_data, dict):
                    for section in content_data.get("lists", []):
                        parts.append(f"\n## {section.get('text', '')}")
                        parts.append(section.get("content", ""))
                elif isinstance(content_data, str):
                    parts.append(content_data)
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": name})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/postings", params={"limit": 1})
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


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
