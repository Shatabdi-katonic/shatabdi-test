"""Front connector.

API: Front Core API v2
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (q[after] filter)
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
_BASE = "https://api2.frontapp.com"


class FrontConnector(ConnectorBase):
    """Native Front connector via Core API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._inbox_ids: list[str] = config.get("inbox_ids", [])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Front requires 'access_token'", connector_type="front")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/me")
            me = resp.json()
            logger.info("Front authenticated as %s", me.get("email", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Front auth failed: {exc}", connector_type="front") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        params: dict = {"limit": 50}
        if since:
            params["q"] = f"after:{int(since.timestamp())}"
        next_url: str | None = None
        while True:
            try:
                if next_url:
                    resp = await self._client.get(next_url)
                else:
                    resp = await self._client.get("/conversations", params=params)
            except Exception as exc:
                _raise_mapped(exc, "front")
                raise
            body = resp.json()
            for conv in body.get("_results", []):
                modified = datetime.fromtimestamp(conv.get("last_message", {}).get("created_at", conv.get("created_at", 0)), tz=UTC)
                yield DocumentMetadata(
                    external_id=conv["id"],
                    title=conv.get("subject", f"Conversation {conv['id']}"),
                    content_type="text/plain",
                    modified_at=modified,
                    metadata={"status": conv.get("status")},
                )
            pagination = body.get("_pagination", {})
            next_url = pagination.get("next")
            if not next_url:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/conversations/{doc_id}")
        except Exception as exc:
            _raise_mapped(exc, "front")
            raise
        conv = resp.json()
        parts = [f"# {conv.get('subject', doc_id)}", ""]
        parts.append(f"**Status:** {conv.get('status', 'N/A')}")
        parts.append("")
        try:
            msg_resp = await self._client.get(f"/conversations/{doc_id}/messages", params={"limit": 50})
            messages = msg_resp.json().get("_results", [])
        except Exception:
            messages = []
        for msg in messages:
            author = (msg.get("author") or {}).get("email", "Unknown")
            parts.append(f"\n**{author}:**")
            if msg.get("body"):
                parts.append(msg["body"])
            elif msg.get("text"):
                parts.append(msg["text"])
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": conv.get("subject", "")})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/me")
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
