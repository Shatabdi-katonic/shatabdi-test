"""Zendesk connector.

API: Zendesk REST API v2
Auth: Email + API token via Basic auth ({email}/token:{api_token})
Sync: Incremental (updated_after filter for tickets) + full for help center articles
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


class ZendeskConnector(ConnectorBase):
    """Native Zendesk connector for tickets and help-center articles.

    Config:
        subdomain: Zendesk subdomain (e.g., "acme" for acme.zendesk.com)
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept the AddKnowledgeModal form keys (zendeskSubdomain/zendeskEmail/
        # zendeskToken) alongside the canonical keys. Without this the subdomain
        # arrived empty → base URL "https://.zendesk.com" → "This help center
        # does not exist". Same field-mapping fallback as Outline/ClickUp/Linear.
        self._subdomain: str = config.get("subdomain") or config.get("zendeskSubdomain") or ""
        self._base_url: str = f"https://{self._subdomain}.zendesk.com/api/v2"
        self._email_fallback: str = config.get("zendeskEmail") or ""
        self._token_fallback: str = config.get("zendeskToken") or ""
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using email + api_token via Basic auth."""
        email = credentials.get("email") or self._email_fallback or ""
        api_token = credentials.get("api_token") or self._token_fallback or ""
        if not self._subdomain:
            raise ConnectorAuthError(
                "Zendesk requires a subdomain ('subdomain' or 'zendeskSubdomain')",
                connector_type="zendesk",
            )
        if not email or not api_token:
            raise ConnectorAuthError(
                "Zendesk requires 'email' and 'api_token' (zendeskEmail/zendeskToken)",
                connector_type="zendesk",
            )

        cred_str = f"{email}/token:{api_token}"
        b64 = base64.b64encode(cred_str.encode()).decode()
        headers = {"Authorization": f"Basic {b64}"}

        self._client = RetryClient(base_url=self._base_url, headers=headers, rate_limiter=self.rate_limiter)

        try:
            data = await self._client.get_json("/users/me.json")
            user = data.get("user", {})
            logger.info("Zendesk authenticated as %s", user.get("email", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(
                f"Zendesk authentication failed: {exc}",
                connector_type="zendesk",
            ) from exc

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Yield help-center articles and tickets.

        Tickets are filtered by updated_after when ``since`` is provided.
        """
        assert self._client is not None

        # --- Help Center articles ---
        page = 1
        while True:
            try:
                data = await self._client.get_json(
                    "/help_center/articles.json",
                    params={"page": str(page), "per_page": "100"},
                )
            except Exception as exc:
                _raise_mapped(exc, "zendesk")
                raise

            articles = data.get("articles", [])
            if not articles:
                break

            for art in articles:
                updated = _parse_ts(art.get("updated_at", ""))
                if since and updated < since:
                    continue
                yield DocumentMetadata(
                    external_id=f"article:{art['id']}",
                    title=art.get("title", ""),
                    url=art.get("html_url"),
                    content_type="text/html",
                    author=art.get("author_id") and str(art["author_id"]),
                    modified_at=updated,
                    metadata={"type": "article", "section_id": art.get("section_id")},
                )

            if not data.get("next_page"):
                break
            page += 1

        # --- Tickets ---
        params: dict[str, str] = {"page": "1", "per_page": "100"}
        if since:
            params["updated_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        page = 1
        while True:
            params["page"] = str(page)
            try:
                data = await self._client.get_json("/tickets.json", params=params)
            except Exception as exc:
                _raise_mapped(exc, "zendesk")
                raise

            tickets = data.get("tickets", [])
            if not tickets:
                break

            for t in tickets:
                yield DocumentMetadata(
                    external_id=f"ticket:{t['id']}",
                    title=f"Ticket #{t['id']}: {t.get('subject', '')}",
                    url=f"https://{self._subdomain}.zendesk.com/agent/tickets/{t['id']}",
                    content_type="text/plain",
                    modified_at=_parse_ts(t.get("updated_at", "")),
                    metadata={
                        "type": "ticket",
                        "status": t.get("status"),
                        "priority": t.get("priority"),
                    },
                )

            if not data.get("next_page"):
                break
            page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch an article or ticket by composite ID (e.g. 'article:123')."""
        assert self._client is not None
        kind, _, raw_id = doc_id.partition(":")

        try:
            if kind == "article":
                data = await self._client.get_json(f"/help_center/articles/{raw_id}.json")
                article = data.get("article", {})
                content = f"# {article.get('title', '')}\n\n{article.get('body', '')}"
                return RawDocument(
                    external_id=doc_id,
                    content=content.encode("utf-8"),
                    content_type="text/html",
                    metadata={"title": article.get("title", "")},
                )

            # Default: ticket
            data = await self._client.get_json(f"/tickets/{raw_id}.json")
            ticket = data.get("ticket", {})
            parts = [f"# Ticket #{raw_id}: {ticket.get('subject', '')}"]
            parts.append(f"\nStatus: {ticket.get('status', '')}")
            parts.append(f"Priority: {ticket.get('priority', '')}")
            if ticket.get("description"):
                parts.append(f"\n{ticket['description']}")

            # Fetch comments
            try:
                cdata = await self._client.get_json(f"/tickets/{raw_id}/comments.json")
                comments = cdata.get("comments", [])
                if comments:
                    parts.append("\n## Comments")
                    for c in comments:
                        parts.append(f"\n**{c.get('author_id', 'Unknown')}:**\n{c.get('body', '')}")
            except Exception as e:
                logger.warning("Failed to fetch comments for ticket %s: %s (document returned without comments)", raw_id, e)

            content = "\n".join(parts)
            return RawDocument(
                external_id=doc_id,
                content=content.encode("utf-8"),
                content_type="text/plain",
                metadata={"title": ticket.get("subject", "")},
            )
        except Exception as exc:
            _raise_mapped(exc, "zendesk")
            raise

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Zendesk does not expose document-level permissions."""
        return []

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/users/me.json")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp from Zendesk."""
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
            retry_after = float(exc.response.headers.get("Retry-After", "5"))
            raise ConnectorRateLimitError(
                str(exc), connector_type=connector_type, retry_after=retry_after
            ) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
