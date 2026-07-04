"""Smartsheet connector.

API: Smartsheet REST API 2.0
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (modifiedAt sort)
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
_BASE = "https://api.smartsheet.com/2.0"


class SmartsheetConnector(ConnectorBase):
    """Native Smartsheet connector via REST API."""

    def __init__(self, config: dict | None = None) -> None:
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Smartsheet requires 'access_token'", connector_type="smartsheet")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/users/me")
            me = resp.json()
            logger.info("Smartsheet authenticated as %s", me.get("email", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Smartsheet auth failed: {exc}", connector_type="smartsheet") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        page = 1
        while True:
            try:
                resp = await self._client.get("/sheets", params={"page": page, "pageSize": 100, "includeAll": "false"})
            except Exception as exc:
                _raise_mapped(exc, "smartsheet")
                raise
            body = resp.json()
            for sheet in body.get("data", []):
                modified = _parse_ts(sheet.get("modifiedAt", ""))
                if since and modified < since:
                    continue
                yield DocumentMetadata(
                    external_id=str(sheet["id"]),
                    title=sheet.get("name", ""),
                    url=sheet.get("permalink"),
                    content_type="text/plain",
                    modified_at=modified,
                    metadata={"accessLevel": sheet.get("accessLevel")},
                )
            total_pages = body.get("totalPages", 1)
            if page >= total_pages:
                break
            page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/sheets/{doc_id}", params={"include": "discussions"})
        except Exception as exc:
            _raise_mapped(exc, "smartsheet")
            raise
        sheet = resp.json()
        parts = [f"# {sheet.get('name', doc_id)}", ""]
        columns = {c["id"]: c.get("title", "") for c in sheet.get("columns", [])}
        parts.append("| " + " | ".join(columns.values()) + " |")
        parts.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in sheet.get("rows", [])[:200]:
            cells = {c.get("columnId"): c.get("displayValue", c.get("value", "")) for c in row.get("cells", [])}
            vals = [str(cells.get(cid, "")) for cid in columns]
            parts.append("| " + " | ".join(vals) + " |")
        discussions = sheet.get("discussions", [])
        if discussions:
            parts.append("\n## Discussions")
            for d in discussions:
                for comment in d.get("comments", []):
                    author = (comment.get("createdBy") or {}).get("name", "Unknown")
                    parts.append(f"\n**{author}:** {comment.get('text', '')}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": sheet.get("name", "")})

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
