"""Figma connector.

API: Figma REST API v1
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (last_modified filter)
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
_BASE = "https://api.figma.com/v1"


class FigmaConnector(ConnectorBase):
    """Native Figma connector via REST API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._team_ids: list[str] = config.get("team_ids", [])
        self._project_ids: list[str] = config.get("project_ids", [])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Figma requires 'access_token'", connector_type="figma")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/me")
            me = resp.json()
            logger.info("Figma authenticated as %s", me.get("handle", me.get("email", "?")))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Figma auth failed: {exc}", connector_type="figma") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        project_ids = list(self._project_ids)
        if self._team_ids and not project_ids:
            for team_id in self._team_ids:
                try:
                    resp = await self._client.get(f"/teams/{team_id}/projects")
                    for proj in resp.json().get("projects", []):
                        project_ids.append(str(proj["id"]))
                except Exception as exc:
                    _raise_mapped(exc, "figma")
                    raise
        if not project_ids:
            try:
                resp = await self._client.get("/me")
                me = resp.json()
                for team in me.get("teams", []):
                    try:
                        proj_resp = await self._client.get(f"/teams/{team['id']}/projects")
                        for proj in proj_resp.json().get("projects", []):
                            project_ids.append(str(proj["id"]))
                    except Exception:
                        continue
            except Exception:
                pass
        for project_id in project_ids:
            try:
                resp = await self._client.get(f"/projects/{project_id}/files")
            except Exception as exc:
                _raise_mapped(exc, "figma")
                raise
            for f in resp.json().get("files", []):
                modified = _parse_ts(f.get("last_modified", ""))
                if since and modified < since:
                    continue
                yield DocumentMetadata(
                    external_id=f["key"],
                    title=f.get("name", ""),
                    url=f"https://www.figma.com/file/{f['key']}",
                    content_type="application/figma",
                    modified_at=modified,
                    folder_id=project_id,
                    metadata={"project_id": project_id},
                )

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/files/{doc_id}", params={"depth": 2})
        except Exception as exc:
            _raise_mapped(exc, "figma")
            raise
        file_data = resp.json()
        parts = [f"# {file_data.get('name', doc_id)}", ""]
        parts.append(f"**Last Modified:** {file_data.get('lastModified', '')}")
        parts.append(f"**Version:** {file_data.get('version', '')}")
        parts.append("")
        document = file_data.get("document", {})
        for page in document.get("children", []):
            parts.append(f"## Page: {page.get('name', '')}")
            for child in page.get("children", [])[:50]:
                parts.append(f"- {child.get('type', '')}: {child.get('name', '')}")
            parts.append("")
        try:
            comments_resp = await self._client.get(f"/files/{doc_id}/comments")
            comments = comments_resp.json().get("comments", [])
            if comments:
                parts.append("## Comments")
                for c in comments[:50]:
                    user = (c.get("user") or {}).get("handle", "Unknown")
                    parts.append(f"\n**{user}:** {c.get('message', '')}")
        except Exception:
            pass
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": file_data.get("name", "")})

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
