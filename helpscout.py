"""Help Scout connector.

API: Help Scout Mailbox API v2
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (modifiedSince filter)
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
_BASE = "https://api.helpscout.net/v2"


class HelpScoutConnector(ConnectorBase):
    """Native Help Scout connector via Mailbox API v2."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._mailbox_ids: list[int] = config.get("mailbox_ids", [])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("HelpScout requires 'access_token'", connector_type="helpscout")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/users/me")
            me = resp.json()
            logger.info("HelpScout authenticated as %s", me.get("email", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"HelpScout auth failed: {exc}", connector_type="helpscout") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        page = 1
        while True:
            params: dict = {"page": page, "status": "all"}
            if since:
                params["modifiedSince"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                resp = await self._client.get("/conversations", params=params)
            except Exception as exc:
                _raise_mapped(exc, "helpscout")
                raise
            body = resp.json()
            embedded = body.get("_embedded", {})
            for conv in embedded.get("conversations", []):
                if self._mailbox_ids and conv.get("mailboxId") not in self._mailbox_ids:
                    continue
                yield DocumentMetadata(
                    external_id=str(conv["id"]),
                    title=conv.get("subject", ""),
                    content_type="text/plain",
                    modified_at=_parse_ts(conv.get("userUpdatedAt", conv.get("updatedAt", ""))),
                    metadata={"status": conv.get("status"), "mailboxId": conv.get("mailboxId")},
                )
            pages = body.get("page", {})
            if page >= pages.get("totalPages", 1):
                break
            page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/conversations/{doc_id}", params={"embed": "threads"})
        except Exception as exc:
            _raise_mapped(exc, "helpscout")
            raise
        conv = resp.json()
        parts = [f"# {conv.get('subject', doc_id)}", ""]
        parts.append(f"**Status:** {conv.get('status', 'N/A')}")
        parts.append(f"**Type:** {conv.get('type', 'N/A')}")
        assignee = conv.get("assignee") or {}
        if assignee:
            parts.append(f"**Assignee:** {assignee.get('first', '')} {assignee.get('last', '')}")
        parts.append("")
        threads = (conv.get("_embedded") or {}).get("threads", [])
        for thread in threads:
            author = ""
            created_by = thread.get("createdBy") or thread.get("customer") or {}
            author = f"{created_by.get('first', '')} {created_by.get('last', '')}".strip() or created_by.get("email", "Unknown")
            parts.append(f"\n**{author} ({thread.get('type', 'reply')}):**")
            if thread.get("body"):
                parts.append(thread["body"])
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": conv.get("subject", "")})

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
