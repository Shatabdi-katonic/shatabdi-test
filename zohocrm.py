"""Zoho CRM connector.

API: Zoho CRM REST API v6
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (Modified_Time filter)
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
_BASE = "https://www.zohoapis.com/crm/v6"


class ZohoCRMConnector(ConnectorBase):
    """Native Zoho CRM connector via REST API v6."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._modules: list[str] = config.get("modules", ["Leads", "Contacts", "Deals"])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("ZohoCRM requires 'access_token'", connector_type="zohocrm")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/users", params={"type": "CurrentUser"})
            users = resp.json().get("users", [])
            if users:
                logger.info("ZohoCRM authenticated as %s", users[0].get("full_name", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"ZohoCRM auth failed: {exc}", connector_type="zohocrm") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        for module in self._modules:
            page = 1
            while True:
                params: dict = {"page": page, "per_page": 200}
                if since:
                    params["If-Modified-Since"] = since.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                try:
                    resp = await self._client.get(f"/{module}", params=params)
                except Exception as exc:
                    _raise_mapped(exc, "zohocrm")
                    raise
                if resp.status_code == 204:
                    break
                body = resp.json()
                records = body.get("data", [])
                for record in records:
                    modified = _parse_ts(record.get("Modified_Time", ""))
                    name = record.get("Full_Name") or record.get("Deal_Name") or record.get("Subject") or f"{module} {record.get('id', '')}"
                    yield DocumentMetadata(
                        external_id=f"{module}_{record['id']}",
                        title=name,
                        content_type="text/plain",
                        modified_at=modified,
                        metadata={"module": module},
                    )
                info = body.get("info", {})
                if not info.get("more_records"):
                    break
                page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        parts_split = doc_id.split("_", 1)
        module = parts_split[0] if len(parts_split) == 2 else "Leads"
        record_id = parts_split[1] if len(parts_split) == 2 else doc_id
        try:
            resp = await self._client.get(f"/{module}/{record_id}")
        except Exception as exc:
            _raise_mapped(exc, "zohocrm")
            raise
        records = resp.json().get("data", [])
        record = records[0] if records else {}
        name = record.get("Full_Name") or record.get("Deal_Name") or record.get("Subject") or doc_id
        parts = [f"# {name}", f"**Module:** {module}", ""]
        skip_keys = {"id", "$currency_symbol", "$field_states", "$approval", "$review", "$zia_owner_assignment",
                      "$sharing_permission", "$approval_state", "$in_merge", "$blueprint_api_content"}
        for key, val in record.items():
            if key.startswith("$") and key in skip_keys:
                continue
            if val is not None and val != "" and not isinstance(val, (dict, list)):
                parts.append(f"**{key.replace('_', ' ')}:** {val}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": name})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/users", params={"type": "CurrentUser"})
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
