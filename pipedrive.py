"""Pipedrive connector.

API: Pipedrive REST API v1
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (update_time sort)
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
_BASE = "https://api.pipedrive.com/v1"


class PipedriveConnector(ConnectorBase):
    """Native Pipedrive connector via REST API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._content_types: list[str] = config.get("content_types", ["deals", "persons", "organizations"])
        self._client: RetryClient | None = None
        # Owner identifier captured at authenticate-time. Used by
        # ``get_permissions`` to write a SpiceDB ``owner`` relation for every
        # ingested Pipedrive entity. Without this, the syncer wrote zero
        # relationships and the retriever permission filter excluded every
        # Pipedrive chunk from search results — exact same root cause as the
        # Miro phantom-chunks bug (see miro.py for the full bug history).
        # Pipedrive's REST API has no per-record ACL endpoint, so we treat
        # every ingested deal/person/organization as owned by the user who
        # registered the credential.
        self._owner_user_id: str = ""

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Pipedrive requires 'access_token'", connector_type="pipedrive")
        # Prefer the canonical platform user_id (Keycloak sub) injected by
        # the OAuth callback as ``platform_user_id``. Falls back to the
        # provider-native ``user_id`` for pre-fix credential records.
        # See miro.py for the full bug history — connectors used to read
        # Pipedrive's numeric user_id, which doesn't match Keycloak UUID
        # format, so IdentityResolver dropped the entry and no SpiceDB
        # relationships were written.
        self._owner_user_id = (
            credentials.get("platform_user_id")
            or credentials.get("user_id")
            or ""
        ).strip()
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/users/me")
            me = resp.json().get("data", {})
            logger.info(
                "Pipedrive authenticated as %s (owner=%s)",
                me.get("name", "?"), self._owner_user_id or "?",
            )
            # Fallback to Pipedrive's native user id when the credential
            # didn't carry the platform user_id. IdentityResolver maps it
            # to the canonical platform user at search-time via the
            # credential-store mapping registered at connector-create time.
            if not self._owner_user_id:
                self._owner_user_id = str(me.get("id") or "").strip()
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Pipedrive auth failed: {exc}", connector_type="pipedrive") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        for entity_type in self._content_types:
            start = 0
            while True:
                params: dict = {"start": start, "limit": 100, "sort": "update_time DESC"}
                try:
                    resp = await self._client.get(f"/{entity_type}", params=params)
                except Exception as exc:
                    _raise_mapped(exc, "pipedrive")
                    raise
                body = resp.json()
                items = body.get("data") or []
                for item in items:
                    modified = _parse_ts(item.get("update_time", ""))
                    if since and modified < since:
                        continue
                    name = item.get("title") or item.get("name") or f"{entity_type} {item.get('id', '')}"
                    yield DocumentMetadata(
                        external_id=f"{entity_type}_{item['id']}",
                        title=name,
                        content_type="text/plain",
                        modified_at=modified,
                        metadata={"type": entity_type},
                    )
                pagination = body.get("additional_data", {}).get("pagination", {})
                if not pagination.get("more_items_in_collection"):
                    break
                start = pagination.get("next_start", start + 100)

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        parts_split = doc_id.split("_", 1)
        entity_type = parts_split[0] if len(parts_split) == 2 else "deals"
        entity_id = parts_split[1] if len(parts_split) == 2 else doc_id
        try:
            resp = await self._client.get(f"/{entity_type}/{entity_id}")
        except Exception as exc:
            _raise_mapped(exc, "pipedrive")
            raise
        item = resp.json().get("data", {})
        name = item.get("title") or item.get("name") or doc_id
        parts = [f"# {name}", ""]
        for key in ["status", "stage_id", "pipeline_id", "value", "currency", "email", "phone", "address"]:
            val = item.get(key)
            if val:
                parts.append(f"**{key.replace('_', ' ').title()}:** {val}")
        if item.get("owner_id"):
            owner = item["owner_id"] if isinstance(item["owner_id"], str) else (item["owner_id"] or {}).get("name", "")
            if owner:
                parts.append(f"**Owner:** {owner}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": name})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Pipedrive's REST API does not expose per-record ACLs.

        Treat every ingested record (deal / person / organization) as
        owned by the user who registered the credential — same pattern
        as miro.py, airtable.py, asana.py, bamboohr.py, and
        file_upload.py:162-172. Without this, the syncer wrote zero
        SpiceDB relationships and the retriever's permission filter
        (retriever.py:626) silently dropped every Pipedrive chunk from
        search results.
        """
        if self._owner_user_id:
            return [
                PermissionEntry(
                    subject_type="user",
                    subject_id=self._owner_user_id,
                    relation="owner",
                )
            ]
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
        try:
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
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
