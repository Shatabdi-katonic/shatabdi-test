"""Basecamp connector.

API: Basecamp 4 REST API
Auth: Bearer access_token (OAuth 2.0)
Sync: Full (no incremental filter on list endpoints)
Permissions: Not supported

Content types indexed:
  - Message Board messages
  - Docs & Files documents (vault, recursively) — CR-613

Multi-account: the OAuth token can grant access to several Basecamp accounts;
all Basecamp-4 ("bc3") accounts are indexed unless an explicit account_id is set.
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
_BASE = "https://3.basecampapi.com"


class BasecampConnector(ConnectorBase):
    """Native Basecamp connector via REST API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._account_id: str = config.get("account_id", "")
        self._project_ids: list[int] = config.get("project_ids", [])
        self._accounts: list[str] = []  # CR-613: all account ids to index
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Basecamp requires 'access_token'", connector_type="basecamp")
        headers = {**bearer_headers(token), "User-Agent": "Katonic KE (https://katonic.ai)"}

        # CR-611 + CR-613: Basecamp is a one-click OAuth provider — the wizard has
        # no account_id field and the OAuth flow returns only an access_token.
        # Discover the account(s) from Basecamp's Launchpad authorization endpoint.
        # CR-613: index ALL Basecamp-4 ("bc3") accounts the token can access (the
        # token often spans several accounts, and the user's content may be in a
        # different one than the first), unless an explicit account_id is set.
        if self._account_id:
            self._accounts = [self._account_id]
        else:
            try:
                disco = RetryClient(base_url="https://launchpad.37signals.com", headers=headers)
                auth_info = (await disco.get("/authorization.json")).json()
                await disco.close()
                for a in (auth_info.get("accounts") or []):
                    if not a.get("id"):
                        continue
                    # CR-613: select by API host (3.basecampapi.com) — the API
                    # this connector speaks — NOT the product label, which varies
                    # ("Basecamp 3"/"4"/"5" all map to the bc3 API family). A
                    # `product == "bc3"` filter would wrongly drop accounts whose
                    # app is registered as "Basecamp 5", etc.
                    href = a.get("href", "") or ""
                    if "3.basecampapi.com" in href or a.get("product") == "bc3":
                        self._accounts.append(str(a["id"]))
                logger.info(
                    "Basecamp auto-discovered %d account(s): %s",
                    len(self._accounts),
                    self._accounts,
                )
            except Exception as exc:
                logger.warning("Basecamp account auto-discovery failed: %s", exc)

        if not self._accounts:
            raise ConnectorAuthError("Basecamp requires 'account_id'", connector_type="basecamp")
        self._account_id = self._accounts[0]
        # Single client rooted at the API host; the account id is part of each path
        # so one client can talk to every account the token can access.
        self._client = RetryClient(base_url=_BASE, headers=headers)
        try:
            resp = await self._client.get(f"/{self._account_id}/projects.json")
            logger.info(
                "Basecamp authenticated, %d account(s), %d projects in first account",
                len(self._accounts),
                len(resp.json()),
            )
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Basecamp auth failed: {exc}", connector_type="basecamp") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        for acc in self._accounts:
            try:
                projects = (await self._client.get(f"/{acc}/projects.json")).json()
            except Exception as exc:
                logger.warning("Basecamp projects fetch failed for account %s: %s", acc, exc)
                continue
            for proj in projects:
                if self._project_ids and proj["id"] not in self._project_ids:
                    continue
                bucket = proj["id"]
                dock = proj.get("dock", [])

                # Message Board messages
                mb = next((d for d in dock if d.get("name") == "message_board"), None)
                if mb and mb.get("id"):
                    try:
                        msgs = (await self._client.get(
                            f"/{acc}/buckets/{bucket}/message_boards/{mb['id']}/messages.json"
                        )).json()
                    except Exception as exc:
                        logger.warning("Basecamp messages fetch failed (acct %s proj %s): %s", acc, bucket, exc)
                        msgs = []
                    for msg in msgs:
                        modified = _parse_ts(msg.get("updated_at", msg.get("created_at", "")))
                        if since and modified < since:
                            continue
                        yield DocumentMetadata(
                            external_id=f"{acc}:message:{bucket}:{msg['id']}",
                            title=msg.get("subject", msg.get("title", "")),
                            url=msg.get("app_url"),
                            content_type="text/html",
                            author=((msg.get("creator") or {}).get("email_address")),
                            modified_at=modified,
                            folder_id=str(bucket),
                            metadata={"project": proj.get("name"), "account": acc, "type": "message"},
                        )

                # CR-613: Docs & Files (vault) documents, recursively
                vault = next((d for d in dock if d.get("name") == "vault"), None)
                if vault and vault.get("id"):
                    async for doc in self._walk_vault(acc, bucket, vault["id"], proj.get("name"), since):
                        yield doc

    async def _walk_vault(
        self, acc: str, bucket: int, vault_id: int, project_name: str | None, since: datetime | None
    ) -> AsyncIterator[DocumentMetadata]:
        """Yield documents in a vault, recursing into sub-vaults (folders)."""
        try:
            docs = (await self._client.get(
                f"/{acc}/buckets/{bucket}/vaults/{vault_id}/documents.json"
            )).json()
        except Exception as exc:
            logger.debug("Basecamp vault documents fetch failed (vault %s): %s", vault_id, exc)
            docs = []
        for d in docs:
            modified = _parse_ts(d.get("updated_at", d.get("created_at", "")))
            if since and modified < since:
                continue
            yield DocumentMetadata(
                external_id=f"{acc}:document:{bucket}:{d['id']}",
                title=d.get("title", ""),
                url=d.get("app_url"),
                content_type="text/html",
                author=((d.get("creator") or {}).get("email_address")),
                modified_at=modified,
                folder_id=str(bucket),
                metadata={"project": project_name, "account": acc, "type": "document"},
            )
        try:
            subvaults = (await self._client.get(
                f"/{acc}/buckets/{bucket}/vaults/{vault_id}/vaults.json"
            )).json()
        except Exception as exc:
            logger.debug("Basecamp sub-vault fetch failed (vault %s): %s", vault_id, exc)
            subvaults = []
        for sv in subvaults:
            if sv.get("id"):
                async for doc in self._walk_vault(acc, bucket, sv["id"], project_name, since):
                    yield doc

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        # CR-613 id format: "{account}:{type}:{bucket}:{id}". Back-compat: a bare
        # numeric id is a legacy message in the first account.
        parts = doc_id.split(":")
        if len(parts) == 4:
            acc, dtype, bucket, rid = parts
        else:
            acc, dtype, bucket, rid = self._account_id, "message", None, doc_id
        try:
            if dtype == "document":
                data = (await self._client.get(f"/{acc}/buckets/{bucket}/documents/{rid}.json")).json()
                title = data.get("title", rid)
                body = data.get("content", "")
            elif bucket:
                data = (await self._client.get(f"/{acc}/buckets/{bucket}/messages/{rid}.json")).json()
                title = data.get("subject", rid)
                body = data.get("content", "")
            else:
                data = (await self._client.get(f"/{acc}/my/messages/{rid}.json")).json()
                title = data.get("subject", rid)
                body = data.get("content", "")
        except Exception as exc:
            _raise_mapped(exc, "basecamp")
            raise
        out = [f"# {title}", ""]
        creator = (data.get("creator") or {}).get("name", "")
        if creator:
            out.append(f"**Author:** {creator}")
            out.append("")
        if body:
            out.append(body)
        content = "\n".join(out)
        return RawDocument(
            external_id=doc_id,
            content=content.encode(),
            content_type="text/html",
            metadata={"title": title},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client or not self._accounts:
            return False
        try:
            await self._client.get(f"/{self._accounts[0]}/projects.json")
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
