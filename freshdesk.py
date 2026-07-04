"""Freshdesk connector.

API: Freshdesk REST API v2
Auth: API key via Basic auth ({api_key}:X)
Sync: Incremental (updated_since filter for tickets) + KB articles
Permissions: Not supported (returns empty)
"""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import RetryClient
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


class FreshdeskConnector(ConnectorBase):
    """Native Freshdesk connector for tickets and solution articles.

    Config:
        domain: Full Freshdesk domain (e.g., "https://acme.freshdesk.com")
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # CR-598: the Add-Knowledge wizard sends the domain as `freshdeskDomain`
        # (knowledgeProviders.js), not `domain`. Accept both, and normalise a
        # bare host ("mycompany.freshdesk.com") to a full https:// base URL so
        # RetryClient's base_url is valid. Without the fallback the base URL was
        # empty and every /api/v2 call hit nowhere.
        _domain = (config.get("domain") or config.get("freshdeskDomain") or "").strip().rstrip("/")
        if _domain and not _domain.startswith(("http://", "https://")):
            _domain = "https://" + _domain
        self._domain: str = _domain
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using api_key via Basic auth ({api_key}:X)."""
        # CR-598: the wizard sends the key as `freshdeskApiKey`; the inline-sync
        # path merges source.config into `credentials`, so accept that key as a
        # fallback to `api_key`. Without this the connector raised "requires
        # 'api_key' credential" and indexed 0 docs even though a key was entered.
        api_key = credentials.get("api_key") or credentials.get("freshdeskApiKey") or ""
        if not api_key:
            raise ConnectorAuthError(
                "Freshdesk requires 'api_key' credential",
                connector_type="freshdesk",
            )

        cred_str = f"{api_key}:X"
        b64 = base64.b64encode(cred_str.encode()).decode()
        headers = {"Authorization": f"Basic {b64}"}

        self._client = RetryClient(base_url=self._domain, headers=headers, rate_limiter=self.rate_limiter)

        try:
            data = await self._client.get_json("/api/v2/agents/me")
            contact = data.get("contact", {})
            logger.info("Freshdesk authenticated as %s", contact.get("email", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(
                f"Freshdesk authentication failed: {exc}",
                connector_type="freshdesk",
            ) from exc

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Yield tickets and solution articles.

        Tickets support ``updated_since`` filtering. KB articles are fully listed.
        """
        assert self._client is not None

        # --- Tickets ---
        page = 1
        while True:
            params: dict[str, str] = {"page": str(page), "per_page": "100"}
            if since:
                params["updated_since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

            try:
                tickets = await self._client.get_json("/api/v2/tickets", params=params)
            except Exception as exc:
                _raise_mapped(exc, "freshdesk")
                raise

            if not isinstance(tickets, list) or not tickets:
                break

            for t in tickets:
                yield DocumentMetadata(
                    external_id=f"ticket:{t['id']}",
                    title=f"Ticket #{t['id']}: {t.get('subject', '')}",
                    url=f"{self._domain}/a/tickets/{t['id']}",
                    content_type="text/plain",
                    modified_at=_parse_ts(t.get("updated_at", "")),
                    metadata={
                        "type": "ticket",
                        "status": t.get("status"),
                        "priority": t.get("priority"),
                        "source": t.get("source"),
                    },
                )

            # Stop when we get fewer results than requested (last page)
            # Note: previously used < 100 which caused infinite loop when
            # the last page had exactly 100 items
            if len(tickets) < 100 or not tickets:
                break
            page += 1

        # --- Solution articles ---
        page = 1
        while True:
            try:
                articles = await self._client.get_json(
                    "/api/v2/solutions/articles",
                    params={"page": str(page), "per_page": "100"},
                )
            except Exception as exc:
                # Some Freshdesk plans don't include solutions
                logger.debug("Could not fetch solution articles: %s", exc)
                break

            if not isinstance(articles, list) or not articles:
                break

            for art in articles:
                updated = _parse_ts(art.get("updated_at", ""))
                if since and updated < since:
                    continue
                yield DocumentMetadata(
                    external_id=f"article:{art['id']}",
                    title=art.get("title", ""),
                    url=art.get("url"),
                    content_type="text/html",
                    modified_at=updated,
                    metadata={
                        "type": "article",
                        "status": art.get("status"),
                        "folder_id": art.get("folder_id"),
                    },
                )

            if len(articles) < 100 or not articles:
                break
            page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a ticket (with conversations) or solution article."""
        assert self._client is not None
        kind, _, raw_id = doc_id.partition(":")

        try:
            if kind == "article":
                data = await self._client.get_json(f"/api/v2/solutions/articles/{raw_id}")
                content = f"# {data.get('title', '')}\n\n{data.get('description', '')}"
                return RawDocument(
                    external_id=doc_id,
                    content=content.encode("utf-8"),
                    content_type="text/html",
                    metadata={"title": data.get("title", "")},
                )

            # Default: ticket with conversations
            data = await self._client.get_json(
                f"/api/v2/tickets/{raw_id}",
                params={"include": "conversations"},
            )
            parts = [f"# Ticket #{raw_id}: {data.get('subject', '')}"]
            parts.append(f"\nStatus: {data.get('status', '')}")
            parts.append(f"Priority: {data.get('priority', '')}")

            if data.get("description_text"):
                parts.append(f"\n{data['description_text']}")

            convos = data.get("conversations", [])
            if convos:
                parts.append("\n## Conversations")
                for c in convos:
                    from_email = c.get("from_email", "Unknown")
                    parts.append(f"\n**{from_email}:**\n{c.get('body_text', c.get('body', ''))}")

            content = "\n".join(parts)
            return RawDocument(
                external_id=doc_id,
                content=content.encode("utf-8"),
                content_type="text/plain",
                metadata={"title": data.get("subject", "")},
            )
        except Exception as exc:
            _raise_mapped(exc, "freshdesk")
            raise

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Freshdesk does not expose document-level permissions."""
        return []

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/api/v2/agents/me")
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
            retry_after = float(exc.response.headers.get("Retry-After", "60"))
            raise ConnectorRateLimitError(
                str(exc), connector_type=connector_type, retry_after=retry_after
            ) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
