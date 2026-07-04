"""Typeform connector.

API: Typeform REST API
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (since filter on responses)
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
_BASE = "https://api.typeform.com"


class TypeformConnector(ConnectorBase):
    """Native Typeform connector via REST API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._workspace_id: str = config.get("workspace_id", "")
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Typeform requires 'access_token'", connector_type="typeform")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            # CR-586: verify auth via /forms (covered by the granted `forms:read`
            # scope) rather than /me. /me requires the `accounts:read` scope,
            # which this connector never requests (scopes are forms:read +
            # responses:read), so /me returned 403 Forbidden and aborted
            # discovery — 0 docs even with a valid token. /forms is what the
            # connector actually uses, so probing it both validates the token
            # and needs no extra scope / re-authorization.
            resp = await self._client.get("/forms", params={"page_size": 1})
            body = resp.json()
            logger.info(
                "Typeform authenticated (%s form(s) visible)",
                body.get("total_items", len(body.get("items", []))),
            )
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Typeform auth failed: {exc}", connector_type="typeform") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        page = 1
        while True:
            params: dict = {"page": page, "page_size": 200}
            if self._workspace_id:
                params["workspace_id"] = self._workspace_id
            try:
                resp = await self._client.get("/forms", params=params)
            except Exception as exc:
                _raise_mapped(exc, "typeform")
                raise
            body = resp.json()
            for form in body.get("items", []):
                modified = _parse_ts(form.get("last_updated_at", ""))
                if since and modified < since:
                    continue
                yield DocumentMetadata(
                    external_id=form["id"],
                    title=form.get("title", ""),
                    url=form.get("_links", {}).get("display"),
                    content_type="text/plain",
                    modified_at=modified,
                    metadata={"status": form.get("settings", {}).get("is_public")},
                )
            if body.get("page_count", 1) <= page:
                break
            page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/forms/{doc_id}")
        except Exception as exc:
            _raise_mapped(exc, "typeform")
            raise
        form = resp.json()
        parts = [f"# {form.get('title', doc_id)}", ""]
        if form.get("welcome_screens"):
            ws = form["welcome_screens"][0]
            parts.append(f"**Welcome:** {ws.get('title', '')}")
            parts.append("")
        parts.append("## Questions")
        for field in form.get("fields", []):
            parts.append(f"- **{field.get('title', '')}** ({field.get('type', '')})")
        parts.append("")
        try:
            resp_data = await self._client.get(f"/forms/{doc_id}/responses", params={"page_size": 25})
            responses = resp_data.json()
            total = responses.get("total_items", 0)
            parts.append(f"## Responses ({total} total)")
            for item in responses.get("items", []):
                answers = item.get("answers", [])
                parts.append(f"\n**Response {item.get('response_id', '')}:**")
                for ans in answers:
                    field_ref = (ans.get("field") or {}).get("ref", "")
                    val = ans.get("text") or ans.get("number") or ans.get("boolean") or ans.get("choice", {}).get("label") or ""
                    parts.append(f"  - {field_ref}: {val}")
        except Exception:
            pass
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": form.get("title", "")})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            # CR-586: probe /forms (forms:read) not /me (needs accounts:read,
            # not granted → 403). Mirrors authenticate().
            await self._client.get("/forms", params={"page_size": 1})
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
