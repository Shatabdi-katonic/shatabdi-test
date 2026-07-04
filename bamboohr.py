"""BambooHR connector.

API: BambooHR REST API v1
Auth: Bearer access_token (OAuth 2.0) or API key via Basic auth
Sync: Full (no incremental filter for employee directory)
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

_EMPLOYEE_FIELDS = "id,firstName,lastName,displayName,jobTitle,department,division,workEmail,workPhone,location,status,hireDate,supervisorEid"


class BambooHRConnector(ConnectorBase):
    """Native BambooHR connector via REST API.

    Config:
        subdomain: BambooHR company subdomain (e.g. 'mycompany' for mycompany.bamboohr.com)
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._subdomain: str = config.get("subdomain", "")
        self._client: RetryClient | None = None
        # Owner identifier captured at authenticate-time. Used by
        # ``get_permissions`` to write a SpiceDB ``owner`` relation for every
        # ingested BambooHR employee record. Without this, the syncer wrote
        # zero relationships and the retriever permission filter
        # (retriever.py:626) silently dropped every BambooHR chunk from
        # search results — same root cause as the Miro bug.
        #
        # BambooHR has no /me endpoint (API-key auth, the key doesn't map
        # to a single user), so we rely on the platform user_id from
        # credentials — set by the credential store when the source was
        # created. If absent, no permission entry is written and the
        # symptom recurs; admins must set the credential's user_id
        # explicitly.
        self._owner_user_id: str = ""

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        subdomain = credentials.get("subdomain", self._subdomain)
        if not token:
            raise ConnectorAuthError("BambooHR requires 'access_token'", connector_type="bamboohr")
        if not subdomain:
            raise ConnectorAuthError("BambooHR requires 'subdomain'", connector_type="bamboohr")
        self._subdomain = subdomain
        # Capture canonical platform user_id (Keycloak sub). For OAuth-style
        # registration, ``platform_user_id`` is injected by oauth.py callback.
        # BambooHR is API-key only with no OAuth flow, so this typically
        # falls through to ``user_id`` if set explicitly when the credential
        # was created. See miro.py for the full bug history.
        self._owner_user_id = (
            credentials.get("platform_user_id")
            or credentials.get("user_id")
            or ""
        ).strip()
        base_url = f"https://api.bamboohr.com/api/gateway.php/{subdomain}/v1"
        self._client = RetryClient(base_url=base_url, headers={**bearer_headers(token), "Accept": "application/json"})
        try:
            resp = await self._client.get("/employees/directory")
            resp.json()
            logger.info(
                "BambooHR authenticated for %s (owner=%s)",
                subdomain, self._owner_user_id or "?",
            )
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"BambooHR auth failed: {exc}", connector_type="bamboohr") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        try:
            resp = await self._client.get("/employees/directory")
        except Exception as exc:
            _raise_mapped(exc, "bamboohr")
            raise
        body = resp.json()
        for emp in body.get("employees", []):
            name = emp.get("displayName") or f"{emp.get('firstName', '')} {emp.get('lastName', '')}"
            yield DocumentMetadata(
                external_id=str(emp["id"]),
                title=name.strip(),
                content_type="text/plain",
                modified_at=datetime.now(UTC),
                metadata={
                    "department": emp.get("department"),
                    "jobTitle": emp.get("jobTitle"),
                    "location": emp.get("location"),
                },
            )

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/employees/{doc_id}", params={"fields": _EMPLOYEE_FIELDS})
        except Exception as exc:
            _raise_mapped(exc, "bamboohr")
            raise
        emp = resp.json()
        name = emp.get("displayName") or f"{emp.get('firstName', '')} {emp.get('lastName', '')}"
        parts = [f"# {name.strip()}", ""]
        for key in ["jobTitle", "department", "division", "workEmail", "workPhone", "location", "status", "hireDate"]:
            val = emp.get(key)
            if val:
                parts.append(f"**{key.replace('_', ' ').title()}:** {val}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": name.strip()})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """BambooHR's REST API does not expose per-employee ACLs.

        Treat every ingested employee record as owned by the user who
        registered the credential — same pattern as miro.py, airtable.py,
        asana.py, and file_upload.py:162-172. Without this, the syncer
        wrote zero SpiceDB relationships and the retriever's permission
        filter silently dropped every BambooHR chunk from search results.
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
            await self._client.get("/employees/directory")
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
