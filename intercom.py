"""Intercom connector.

API: Intercom REST API v2.10
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (updated_at filter via search)
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
_BASE = "https://api.intercom.io"


class IntercomConnector(ConnectorBase):
    """Native Intercom connector via REST API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._content_types: list[str] = config.get("content_types", ["articles", "conversations"])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Intercom requires 'access_token'", connector_type="intercom")
        headers = {**bearer_headers(token), "Intercom-Version": "2.10"}
        self._client = RetryClient(base_url=_BASE, headers=headers)
        try:
            resp = await self._client.get("/me")
            me = resp.json()
            logger.info("Intercom authenticated as %s", me.get("name", me.get("email", "?")))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Intercom auth failed: {exc}", connector_type="intercom") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        if "articles" in self._content_types:
            async for doc in self._list_articles(since):
                yield doc
        if "conversations" in self._content_types:
            async for doc in self._list_conversations(since):
                yield doc

    async def _list_articles(self, since: datetime | None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        page = 1
        while True:
            try:
                resp = await self._client.get("/articles", params={"page": page, "per_page": 50})
            except Exception as exc:
                _raise_mapped(exc, "intercom")
                raise
            body = resp.json()
            for article in body.get("data", []):
                modified = datetime.fromtimestamp(article.get("updated_at", 0), tz=UTC) if article.get("updated_at") else datetime.now(UTC)
                if since and modified < since:
                    continue
                yield DocumentMetadata(
                    external_id=f"article_{article['id']}",
                    title=article.get("title", ""),
                    url=article.get("url"),
                    content_type="text/html",
                    author=((article.get("author") or {}).get("email")),
                    modified_at=modified,
                    metadata={"type": "article", "state": article.get("state")},
                )
            pages = body.get("pages", {})
            if page >= pages.get("total_pages", 1):
                break
            page += 1

    async def _list_conversations(self, since: datetime | None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        starting_after = None
        while True:
            params: dict = {"per_page": 50}
            if starting_after:
                params["starting_after"] = starting_after
            try:
                resp = await self._client.get("/conversations", params=params)
            except Exception as exc:
                _raise_mapped(exc, "intercom")
                raise
            body = resp.json()
            for conv in body.get("conversations", []):
                modified = datetime.fromtimestamp(conv.get("updated_at", 0), tz=UTC) if conv.get("updated_at") else datetime.now(UTC)
                if since and modified < since:
                    continue
                title = (conv.get("source") or {}).get("subject", f"Conversation {conv['id']}")
                yield DocumentMetadata(
                    external_id=f"conv_{conv['id']}",
                    title=title,
                    content_type="text/plain",
                    modified_at=modified,
                    metadata={"type": "conversation", "state": conv.get("state")},
                )
            pages = body.get("pages", {})
            next_page = (pages.get("next") or {}).get("starting_after")
            if not next_page:
                break
            starting_after = next_page

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        if doc_id.startswith("article_"):
            return await self._fetch_article(doc_id.removeprefix("article_"))
        if doc_id.startswith("conv_"):
            return await self._fetch_conversation(doc_id.removeprefix("conv_"))
        return await self._fetch_article(doc_id)

    async def _fetch_article(self, article_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/articles/{article_id}")
        except Exception as exc:
            _raise_mapped(exc, "intercom")
            raise
        article = resp.json()
        parts = [f"# {article.get('title', article_id)}", ""]
        author = (article.get("author") or {}).get("name", "")
        if author:
            parts.append(f"**Author:** {author}")
        parts.append("")
        if article.get("body"):
            parts.append(article["body"])
        content = "\n".join(parts)
        return RawDocument(external_id=f"article_{article_id}", content=content.encode(), content_type="text/html", metadata={"title": article.get("title", "")})

    async def _fetch_conversation(self, conv_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/conversations/{conv_id}")
        except Exception as exc:
            _raise_mapped(exc, "intercom")
            raise
        conv = resp.json()
        source = conv.get("source", {})
        parts = [f"# {source.get('subject', f'Conversation {conv_id}')}", ""]
        parts.append(f"**State:** {conv.get('state', 'N/A')}")
        parts.append("")
        if source.get("body"):
            parts.append(source["body"])
            parts.append("")
        for part in (conv.get("conversation_parts") or {}).get("conversation_parts", []):
            author = (part.get("author") or {}).get("name", "Unknown")
            parts.append(f"\n**{author}:**\n{part.get('body', '')}")
        content = "\n".join(parts)
        return RawDocument(external_id=f"conv_{conv_id}", content=content.encode(), content_type="text/plain", metadata={"title": source.get("subject", "")})

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
