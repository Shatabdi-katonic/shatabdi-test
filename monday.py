"""Monday.com connector.

API: Monday.com GraphQL API v2
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (updated_at filter) via cursor pagination
Permissions: Not supported (returns empty)
"""

from __future__ import annotations

import json
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

_API_URL = "https://api.monday.com/v2"


class MondayConnector(ConnectorBase):
    """Native Monday.com connector using GraphQL API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._board_ids: list[int] = config.get("board_ids", [])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Monday requires 'access_token'", connector_type="monday")
        self._client = RetryClient(base_url="", headers=bearer_headers(token))
        try:
            result = await self._gql("{ me { id name email } }")
            logger.info("Monday authenticated as %s", result.get("me", {}).get("name", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Monday auth failed: {exc}", connector_type="monday") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        page = 1
        query = """
        query($page: Int!, $boardIds: [ID!]) {
          boards(ids: $boardIds, page: $page, limit: 50) {
            id name
            items_page(limit: 100) {
              cursor
              items { id name updated_at column_values { id text } group { title } }
            }
          }
        }
        """ if self._board_ids else """
        query($page: Int!) {
          boards(page: $page, limit: 50) {
            id name
            items_page(limit: 100) {
              cursor
              items { id name updated_at column_values { id text } group { title } }
            }
          }
        }
        """
        while True:
            variables: dict = {"page": page}
            if self._board_ids:
                variables["boardIds"] = [str(b) for b in self._board_ids]
            try:
                data = await self._gql(query, variables)
            except Exception as exc:
                _raise_mapped(exc, "monday")
                raise
            boards = data.get("boards", [])
            if not boards:
                break
            for board in boards:
                items_page = board.get("items_page", {})
                for item in items_page.get("items", []):
                    modified = _parse_ts(item.get("updated_at", ""))
                    if since and modified < since:
                        continue
                    yield DocumentMetadata(
                        external_id=item["id"],
                        title=item.get("name", ""),
                        url=f"https://monday.com/boards/{board['id']}/pulses/{item['id']}",
                        content_type="text/plain",
                        modified_at=modified,
                        folder_id=board["id"],
                        metadata={"board": board.get("name"), "group": (item.get("group") or {}).get("title")},
                    )
            page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        query = """
        query($ids: [ID!]!) {
          items(ids: $ids) {
            id name updated_at
            board { name }
            group { title }
            column_values { text column { title } }
            updates(limit: 50) { body creator { name } created_at }
          }
        }
        """
        try:
            data = await self._gql(query, {"ids": [doc_id]})
        except Exception as exc:
            _raise_mapped(exc, "monday")
            raise
        items = data.get("items", [])
        item = items[0] if items else {}
        parts = [f"# {item.get('name', doc_id)}"]
        board = (item.get("board") or {}).get("name", "")
        group = (item.get("group") or {}).get("title", "")
        if board:
            parts.append(f"**Board:** {board}")
        if group:
            parts.append(f"**Group:** {group}")
        parts.append("")
        for col in item.get("column_values", []):
            if col.get("text"):
                col_title = (col.get("column") or {}).get("title", "")
                parts.append(f"**{col_title}:** {col['text']}")
        updates = item.get("updates", [])
        if updates:
            parts.append("\n## Updates")
            for u in updates:
                author = (u.get("creator") or {}).get("name", "Unknown")
                parts.append(f"\n**{author}:**\n{u.get('body', '')}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": item.get("name", "")})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._gql("{ me { id } }")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        assert self._client is not None
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = await self._client.post(_API_URL, json=payload)
        body = resp.json()
        if "errors" in body:
            msg = body["errors"][0].get("message", str(body["errors"])) if body["errors"] else "Unknown"
            raise RuntimeError(f"Monday GraphQL error: {msg}")
        return body.get("data", {})


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
            retry_after = float(exc.response.headers.get("Retry-After", "5"))
            raise ConnectorRateLimitError(str(exc), connector_type=connector_type, retry_after=retry_after) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
