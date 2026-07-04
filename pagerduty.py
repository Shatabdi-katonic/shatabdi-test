"""PagerDuty connector.

API: PagerDuty REST API v2
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (since filter on incidents)
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
_BASE = "https://api.pagerduty.com"


class PagerDutyConnector(ConnectorBase):
    """Native PagerDuty connector via REST API v2."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._service_ids: list[str] = config.get("service_ids", [])
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("PagerDuty requires 'access_token'", connector_type="pagerduty")
        self._client = RetryClient(base_url=_BASE, headers={**bearer_headers(token), "Content-Type": "application/json"})
        # CR-615: verify the token with `/abilities`, NOT `/users/me`. PagerDuty's
        # `/users/me` is only accessible with a *user-level API token* and returns
        # 403 for OAuth access tokens — so the old check made every OAuth-connected
        # source fail auth ("403 Forbidden for /users/me") even though the token was
        # valid. `/abilities` is a lightweight endpoint any valid read token can hit.
        try:
            await self._client.get("/abilities")
            logger.info("PagerDuty authenticated (abilities ok)")
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"PagerDuty auth failed: {exc}", connector_type="pagerduty") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        offset = 0
        limit = 100
        while True:
            params: dict = {"offset": offset, "limit": limit, "sort_by": "created_at:desc"}
            if since:
                params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            if self._service_ids:
                params["service_ids[]"] = self._service_ids
            try:
                resp = await self._client.get("/incidents", params=params)
            except Exception as exc:
                _raise_mapped(exc, "pagerduty")
                raise
            body = resp.json()
            incidents = body.get("incidents", [])
            for inc in incidents:
                yield DocumentMetadata(
                    external_id=inc["id"],
                    title=inc.get("title", inc.get("summary", "")),
                    url=inc.get("html_url"),
                    content_type="text/plain",
                    modified_at=_parse_ts(inc.get("last_status_change_at", inc.get("created_at", ""))),
                    metadata={
                        "status": inc.get("status"),
                        "urgency": inc.get("urgency"),
                        "service": (inc.get("service") or {}).get("summary"),
                    },
                )
            if not body.get("more"):
                break
            offset += limit

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/incidents/{doc_id}", params={"include[]": "acknowledgers,assignees"})
        except Exception as exc:
            _raise_mapped(exc, "pagerduty")
            raise
        inc = resp.json().get("incident", {})
        parts = [f"# {inc.get('title', doc_id)}", ""]
        parts.append(f"**Status:** {inc.get('status', 'N/A')}")
        parts.append(f"**Urgency:** {inc.get('urgency', 'N/A')}")
        service = (inc.get("service") or {}).get("summary", "")
        if service:
            parts.append(f"**Service:** {service}")
        parts.append(f"**Created:** {inc.get('created_at', '')}")
        parts.append("")
        if inc.get("description"):
            parts.append(inc["description"])
            parts.append("")
        try:
            log_resp = await self._client.get(f"/incidents/{doc_id}/log_entries", params={"limit": 50})
            for entry in log_resp.json().get("log_entries", []):
                channel = (entry.get("channel") or {}).get("summary", entry.get("type", ""))
                agent = (entry.get("agent") or {}).get("summary", "System")
                parts.append(f"- **{agent}** ({channel}): {entry.get('summary', '')}")
        except Exception:
            pass
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": inc.get("title", "")})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            # CR-615: /abilities, not /users/me (OAuth tokens 403 on /users/me).
            await self._client.get("/abilities")
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
