"""Coda connector.

API: Coda REST API v1
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (updatedAt sort)
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
_BASE = "https://coda.io/apis/v1"


class CodaConnector(ConnectorBase):
    """Native Coda connector via REST API."""

    def __init__(self, config: dict | None = None) -> None:
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        # CR-599: Coda accepts a personal API token (coda.io/account → API
        # settings) used as a Bearer token — the same shape an OAuth access
        # token has. The Add-Knowledge wizard collects it as `codaToken`
        # (knowledgeProviders.js, auth_type=api_key) and the inline-sync path
        # merges source.config into `credentials`, so accept that key (and the
        # generic api_token/api_key) as fallbacks to `access_token` (the OAuth
        # path). Without this the connector raised "requires 'access_token'" and
        # indexed 0 docs even though a token was entered.
        token = (
            credentials.get("access_token")
            or credentials.get("codaToken")
            or credentials.get("api_token")
            or credentials.get("api_key")
            or ""
        )
        if not token:
            raise ConnectorAuthError("Coda requires 'access_token'", connector_type="coda")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/whoami")
            me = resp.json()
            logger.info("Coda authenticated as %s", me.get("name", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Coda auth failed: {exc}", connector_type="coda") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        page_token: str | None = None
        while True:
            params: dict = {"limit": 50}
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = await self._client.get("/docs", params=params)
            except Exception as exc:
                _raise_mapped(exc, "coda")
                raise
            body = resp.json()
            for doc in body.get("items", []):
                modified = _parse_ts(doc.get("updatedAt", ""))
                if since and modified < since:
                    continue
                # CR-617: Coda's `owner` is the owner's email STRING, not an
                # object — `(owner or {}).get("email")` raised "'str' object has
                # no attribute 'get'" on the first doc and aborted discovery.
                _owner = doc.get("owner")
                yield DocumentMetadata(
                    external_id=doc["id"],
                    title=doc.get("name", ""),
                    url=doc.get("browserLink"),
                    content_type="text/plain",
                    author=_owner if isinstance(_owner, str) else ((_owner or {}).get("email")),
                    modified_at=modified,
                    folder_id=doc.get("folderId"),
                    metadata={"type": doc.get("type")},
                )
            page_token = body.get("nextPageToken")
            if not page_token:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/docs/{doc_id}")
        except Exception as exc:
            _raise_mapped(exc, "coda")
            raise
        doc = resp.json()
        parts = [f"# {doc.get('name', doc_id)}", ""]
        # CR-617: Coda's `owner` is an email string; the display name is
        # `ownerName`. The old `(owner or {}).get("name")` crashed on the string.
        _owner = doc.get("owner")
        owner = doc.get("ownerName") or (_owner if isinstance(_owner, str) else (_owner or {}).get("name", "")) or ""
        if owner:
            parts.append(f"**Owner:** {owner}")
        parts.append("")
        try:
            pages_resp = await self._client.get(f"/docs/{doc_id}/pages", params={"limit": 100})
            for page in pages_resp.json().get("items", []):
                parts.append(f"## {page.get('name', '')}")
                try:
                    content_resp = await self._client.get(f"/docs/{doc_id}/pages/{page['id']}/export", params={"outputFormat": "markdown"})
                    parts.append(content_resp.text)
                except Exception:
                    parts.append("*(content not available)*")
                parts.append("")
        except Exception:
            pass
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": doc.get("name", "")})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/whoami")
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
