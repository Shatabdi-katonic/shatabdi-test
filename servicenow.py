"""ServiceNow connector.

API: ServiceNow Table API (REST)
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (sys_updated_on filter)
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


class ServiceNowConnector(ConnectorBase):
    """Native ServiceNow connector via Table API.

    Config:
        subdomain: ServiceNow instance subdomain (e.g. 'mycompany' for mycompany.service-now.com)
        tables: List of tables to sync (default: incident, kb_knowledge)
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._subdomain: str = config.get("subdomain", "")
        self._tables: list[str] = config.get("tables", ["incident", "kb_knowledge"])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        subdomain = credentials.get("subdomain", self._subdomain)
        if not token:
            raise ConnectorAuthError("ServiceNow requires 'access_token'", connector_type="servicenow")
        if not subdomain:
            raise ConnectorAuthError("ServiceNow requires 'subdomain'", connector_type="servicenow")
        self._subdomain = subdomain
        base_url = f"https://{subdomain}.service-now.com/api/now"
        self._client = RetryClient(base_url=base_url, headers={**bearer_headers(token), "Accept": "application/json"})
        try:
            resp = await self._client.get("/table/sys_user", params={"sysparm_query": "user_name=admin", "sysparm_limit": "1"})
            resp.json()
            logger.info("ServiceNow authenticated for instance %s", subdomain)
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"ServiceNow auth failed: {exc}", connector_type="servicenow") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        for table in self._tables:
            offset = 0
            limit = 100
            while True:
                params: dict = {
                    "sysparm_limit": str(limit),
                    "sysparm_offset": str(offset),
                    "sysparm_fields": "sys_id,short_description,number,sys_updated_on,sys_created_by,sys_class_name",
                    "sysparm_display_value": "true",
                }
                if since:
                    params["sysparm_query"] = f"sys_updated_on>={since.strftime('%Y-%m-%d %H:%M:%S')}"
                try:
                    resp = await self._client.get(f"/table/{table}", params=params)
                except Exception as exc:
                    _raise_mapped(exc, "servicenow")
                    raise
                records = resp.json().get("result", [])
                if not records:
                    break
                for rec in records:
                    yield DocumentMetadata(
                        external_id=f"{table}_{rec['sys_id']}",
                        title=rec.get("short_description") or rec.get("number") or rec["sys_id"],
                        url=f"https://{self._subdomain}.service-now.com/{table}.do?sys_id={rec['sys_id']}",
                        content_type="text/plain",
                        author=rec.get("sys_created_by"),
                        modified_at=_parse_snow_ts(rec.get("sys_updated_on", "")),
                        metadata={"table": table, "number": rec.get("number")},
                    )
                if len(records) < limit:
                    break
                offset += limit

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        parts_split = doc_id.split("_", 1)
        table = parts_split[0] if len(parts_split) == 2 else "incident"
        sys_id = parts_split[1] if len(parts_split) == 2 else doc_id
        try:
            resp = await self._client.get(f"/table/{table}/{sys_id}", params={"sysparm_display_value": "true"})
        except Exception as exc:
            _raise_mapped(exc, "servicenow")
            raise
        rec = resp.json().get("result", {})
        title = rec.get("short_description") or rec.get("number") or doc_id
        parts = [f"# {title}", ""]
        for key in ["number", "state", "priority", "category", "assigned_to", "opened_by", "sys_created_on", "description"]:
            val = rec.get(key)
            if val:
                parts.append(f"**{key.replace('_', ' ').title()}:** {val}")
        if rec.get("work_notes"):
            parts.append(f"\n## Work Notes\n{rec['work_notes']}")
        if rec.get("comments"):
            parts.append(f"\n## Comments\n{rec['comments']}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": title})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/table/incident", params={"sysparm_limit": "1"})
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _parse_snow_ts(ts: str) -> datetime:
    if not ts:
        return datetime.now(UTC)
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
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
