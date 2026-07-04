"""Airtable connector.

API: Airtable REST API
Auth: Bearer personal_access_token
Sync: Full sync of records from a base/table, with offset pagination
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

_API_BASE = "https://api.airtable.com/v0"


class AirtableConnector(ConnectorBase):
    """Native Airtable connector.

    Config:
        base_id: Airtable base ID (e.g., "appXXXXXXXXX")
        table_id: Optional table name or ID. If omitted, all tables are synced.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._base_id: str = config.get("base_id", "")
        self._table_id: str | None = config.get("table_id")
        self._client: RetryClient | None = None
        # Owner identifier captured at authenticate-time. Used by
        # ``get_permissions`` to write a SpiceDB ``owner`` relation for every
        # ingested Airtable record. Without this the syncer wrote zero
        # relationships, the retriever's permission filter excluded every
        # Airtable chunk from search results, and ingestion looked successful
        # while ``/search`` returned 0. See miro.py for the same pattern;
        # symptom is Postgres ``total_chunks > 0`` with zero hits.
        # Prefer the platform user_id from credentials (set by the credential
        # store when the source was created) and fall back to the Airtable
        # /meta/whoami user id captured during authenticate.
        self._owner_user_id: str = ""

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using a personal access token."""
        token = credentials.get("personal_access_token") or credentials.get("api_key", "")
        if not token:
            raise ConnectorAuthError(
                "Airtable requires 'personal_access_token' credential",
                connector_type="airtable",
            )

        # First preference: the canonical platform user_id (Keycloak sub)
        # injected by the OAuth callback as ``platform_user_id``. Falls back
        # to ``user_id`` for non-OAuth credentials or pre-fix records.
        # See miro.py for the full bug history — Airtable PATs typically
        # don't have a user_id in the credential, so the /meta/whoami
        # fallback below is the practical path.
        self._owner_user_id = (
            credentials.get("platform_user_id")
            or credentials.get("user_id")
            or ""
        ).strip()

        headers = bearer_headers(token)
        self._client = RetryClient(base_url=_API_BASE, headers=headers, rate_limiter=self.rate_limiter)

        # Verify access by listing bases — and capture the Airtable user id
        # as a fallback when credentials didn't carry the platform user_id.
        # IdentityResolver maps the Airtable native id to the canonical
        # platform user via the credential-store mapping registered at
        # connector-create time.
        try:
            whoami = await self._client.get_json("/meta/whoami")
            if not self._owner_user_id:
                self._owner_user_id = str(whoami.get("id") or "").strip()
            logger.info("Airtable authenticated successfully (owner=%s)", self._owner_user_id or "?")
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(
                f"Airtable authentication failed: {exc}",
                connector_type="airtable",
            ) from exc

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List records from the configured base/table(s).

        If table_id is not set, discovers all tables in the base first.
        """
        assert self._client is not None

        table_ids = await self._get_table_ids()

        for table_name in table_ids:
            offset: str | None = None
            while True:
                params: dict[str, str] = {"pageSize": "100", "returnFieldsByFieldId": "false"}
                if offset:
                    params["offset"] = offset
                # Filter by last modified time for incremental sync
                if since:
                    iso = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                    params["filterByFormula"] = f"LAST_MODIFIED_TIME()>'{iso}'"

                try:
                    data = await self._client.get_json(
                        f"/{self._base_id}/{table_name}",
                        params=params,
                    )
                except Exception as exc:
                    _raise_mapped(exc, "airtable")
                    raise

                records = data.get("records", [])
                for rec in records:
                    fields = rec.get("fields", {})
                    # Airtable API only exposes createdTime in record metadata.
                    # For incremental sync, we use filterByFormula with LAST_MODIFIED_TIME()
                    # to filter server-side. The modified_at here is approximate.
                    created = rec.get("createdTime", "")
                    modified_at = _parse_ts(created)

                    # Use first text field as title or fall back to record ID
                    title = ""
                    for v in fields.values():
                        if isinstance(v, str) and v.strip():
                            title = v
                            break
                    if not title:
                        title = rec["id"]

                    if since and modified_at < since:
                        continue

                    yield DocumentMetadata(
                        external_id=f"{table_name}:{rec['id']}",
                        title=title,
                        url=f"https://airtable.com/{self._base_id}/{table_name}/{rec['id']}",
                        content_type="application/json",
                        modified_at=modified_at,
                        metadata={
                            "table": table_name,
                            "field_count": len(fields),
                        },
                    )

                offset = data.get("offset")
                if not offset:
                    break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single record. doc_id format: '{table_name}:{record_id}'."""
        assert self._client is not None
        table_name, _, record_id = doc_id.partition(":")

        try:
            data = await self._client.get_json(f"/{self._base_id}/{table_name}/{record_id}")
        except Exception as exc:
            _raise_mapped(exc, "airtable")
            raise

        fields = data.get("fields", {})
        # Serialize fields to human-readable text
        lines = [f"# Record {record_id} (Table: {table_name})", ""]
        for key, value in fields.items():
            if isinstance(value, list):
                value_str = ", ".join(str(v) for v in value)
            elif isinstance(value, dict):
                value_str = json.dumps(value, indent=2)
            else:
                value_str = str(value)
            lines.append(f"**{key}:** {value_str}")

        content = "\n".join(lines)
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={"table": table_name},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Airtable does not expose document-level permissions via API.

        Treat every ingested record as owned by the user who registered the
        credential (the source creator) — same pattern as miro.py and
        file_upload.py:162-172. The platform user_id from the credential
        store is preferred; the Airtable native id from /meta/whoami is the
        fallback when credentials didn't carry user_id.

        Without this, the syncer wrote zero relationships and the retriever
        permission filter (retriever.py:626) silently dropped every Airtable
        chunk from search results.
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
        if self._client is None:
            return False
        try:
            await self._client.get_json("/meta/whoami")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_table_ids(self) -> list[str]:
        """Return the list of table names/IDs to sync."""
        assert self._client is not None

        if self._table_id:
            return [self._table_id]

        # Discover tables from the base metadata
        try:
            data = await self._client.get_json(f"/meta/bases/{self._base_id}/tables")
            tables = data.get("tables", [])
            return [t.get("id", t.get("name", "")) for t in tables if t]
        except Exception as exc:
            logger.warning("Failed to list Airtable tables, falling back to base_id: %s", exc)
            return [self._base_id]


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp from Airtable."""
    if not ts:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _raise_mapped(exc: Exception, connector_type: str) -> None:
    """Re-raise httpx errors as connector-specific exceptions."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            raise ConnectorAuthError(str(exc), connector_type=connector_type) from exc
        if code == 429:
            retry_after = float(exc.response.headers.get("Retry-After", "30"))
            raise ConnectorRateLimitError(
                str(exc), connector_type=connector_type, retry_after=retry_after
            ) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
