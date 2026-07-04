"""Wrike connector.

API: Wrike REST API v4
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (updatedDate filter)
Permissions: Not supported
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers
from platform_knowledge_engine.connectors.base import (
    ConnectorAuthError, ConnectorBase, ConnectorRateLimitError,
    ConnectorTransientError, DocumentMetadata, PermissionEntry, RawDocument,
)

logger = logging.getLogger(__name__)
_BASE = "https://www.wrike.com/api/v4"


class WrikeConnector(ConnectorBase):
    """Native Wrike connector via REST API v4."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._folder_ids: list[str] = config.get("folder_ids", [])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Wrike requires 'access_token'", connector_type="wrike")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/contacts", params={"me": "true"})
            me = resp.json().get("data", [{}])[0]
            logger.info("Wrike authenticated as %s %s", me.get("firstName", ""), me.get("lastName", ""))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Wrike auth failed: {exc}", connector_type="wrike") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        params: dict = {"pageSize": 100, "fields": '["description"]'}
        if since:
            params["updatedDate"] = json.dumps({"start": since.strftime("%Y-%m-%dT%H:%M:%SZ")})
        next_page = None
        while True:
            try:
                if next_page:
                    resp = await self._client.get("/tasks", params={**params, "nextPageToken": next_page})
                else:
                    resp = await self._client.get("/tasks", params=params)
            except Exception as exc:
                _raise_mapped(exc, "wrike")
                raise
            body = resp.json()
            for task in body.get("data", []):
                yield DocumentMetadata(
                    external_id=task["id"],
                    title=task.get("title", ""),
                    url=task.get("permalink"),
                    content_type="text/plain",
                    modified_at=_parse_ts(task.get("updatedDate", "")),
                    metadata={"status": task.get("status"), "importance": task.get("importance")},
                )
            next_page = body.get("nextPageToken")
            if not next_page:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            # NOTE: "hasAttachments" is NOT a valid value for Wrike's `fields`
            # parameter (valid options are "attachments"/"attachmentCount"), and
            # passing it makes Wrike reject the whole request with 400 Bad
            # Request — every fetch failed and produced 0 chunks. We only use
            # the description below, so request just that (matches list_documents).
            resp = await self._client.get(
                f"/tasks/{doc_id}",
                params={"fields": '["description"]'},
            )
        except Exception as exc:
            _raise_mapped(exc, "wrike")
            raise
        tasks = resp.json().get("data", [])
        task = tasks[0] if tasks else {}
        parts = [f"# {task.get('title', doc_id)}", ""]
        parts.append(f"**Status:** {task.get('status', 'N/A')}")
        parts.append(f"**Importance:** {task.get('importance', 'N/A')}")
        if task.get("responsibleIds"):
            parts.append(f"**Assignees:** {len(task['responsibleIds'])} assigned")
        parts.append("")
        if task.get("description"):
            parts.append(task["description"])
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": task.get("title", "")})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/contacts", params={"me": "true"})
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
