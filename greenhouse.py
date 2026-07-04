"""Greenhouse connector.

API: Greenhouse Harvest API v1
Auth: Bearer access_token (OAuth 2.0) or Basic auth with API key
Sync: Incremental (updated_after filter)
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
_BASE = "https://harvest.greenhouse.io/v1"


class GreenhouseConnector(ConnectorBase):
    """Native Greenhouse connector via Harvest API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._content_types: list[str] = config.get("content_types", ["candidates", "jobs"])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Greenhouse requires 'access_token'", connector_type="greenhouse")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/users", params={"per_page": 1})
            resp.json()
            logger.info("Greenhouse authenticated")
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Greenhouse auth failed: {exc}", connector_type="greenhouse") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        for entity_type in self._content_types:
            page = 1
            while True:
                params: dict = {"per_page": 100, "page": page}
                if since:
                    params["updated_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    resp = await self._client.get(f"/{entity_type}", params=params)
                except Exception as exc:
                    _raise_mapped(exc, "greenhouse")
                    raise
                items = resp.json()
                if not items:
                    break
                for item in items:
                    if entity_type == "candidates":
                        name = f"{item.get('first_name', '')} {item.get('last_name', '')}".strip()
                    else:
                        name = item.get("name") or item.get("title") or f"{entity_type} {item.get('id', '')}"
                    yield DocumentMetadata(
                        external_id=f"{entity_type}_{item['id']}",
                        title=name,
                        content_type="text/plain",
                        modified_at=_parse_ts(item.get("updated_at", "")),
                        metadata={"type": entity_type},
                    )
                link_header = resp.headers.get("Link", "")
                if 'rel="next"' not in link_header:
                    break
                page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        parts_split = doc_id.split("_", 1)
        entity_type = parts_split[0] if len(parts_split) == 2 else "candidates"
        entity_id = parts_split[1] if len(parts_split) == 2 else doc_id
        try:
            resp = await self._client.get(f"/{entity_type}/{entity_id}")
        except Exception as exc:
            _raise_mapped(exc, "greenhouse")
            raise
        item = resp.json()
        if entity_type == "candidates":
            name = f"{item.get('first_name', '')} {item.get('last_name', '')}".strip()
            parts = [f"# {name}", ""]
            if item.get("title"):
                parts.append(f"**Title:** {item['title']}")
            if item.get("company"):
                parts.append(f"**Company:** {item['company']}")
            for email in item.get("email_addresses", []):
                parts.append(f"**Email:** {email.get('value', '')}")
            apps = item.get("applications", [])
            if apps:
                parts.append("\n## Applications")
                for app in apps:
                    status = app.get("status", "")
                    jobs = [j.get("name", "") for j in app.get("jobs", [])]
                    parts.append(f"- {', '.join(jobs)} ({status})")
        else:
            name = item.get("name") or doc_id
            parts = [f"# {name}", ""]
            for key in ["status", "departments", "offices"]:
                val = item.get(key)
                if val:
                    if isinstance(val, list):
                        val = ", ".join(v.get("name", str(v)) if isinstance(v, dict) else str(v) for v in val)
                    parts.append(f"**{key.title()}:** {val}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": name})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/users", params={"per_page": 1})
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
