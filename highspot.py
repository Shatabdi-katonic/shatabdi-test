"""Highspot connector.

API: Highspot REST API v1
Auth: OAuth 2.0 client_credentials (client_id + client_secret)
Sync: Incremental (modified_after filter) + full
Permissions: Not supported (Highspot uses org-level access)

Content types indexed:
  - Sales enablement items (pitch decks, battle cards, case studies, etc.)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

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

HIGHSPOT_API = "https://api.highspot.com"
HIGHSPOT_TOKEN_URL = "https://api.highspot.com/oauth/token"


class HighspotConnector(ConnectorBase):
    """Highspot connector for sales enablement content."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._page_size: int = config.get("page_size", 50)
        self._client: httpx.AsyncClient | None = None
        self._client_id: str = ""
        self._client_secret: str = ""
        self._access_token: str = ""

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with Highspot using OAuth client_credentials flow."""
        self._client_id = credentials.get("client_id", "")
        self._client_secret = credentials.get("client_secret", "")

        if not self._client_id or not self._client_secret:
            raise ConnectorAuthError(
                "Highspot connector requires client_id and client_secret",
                connector_type="highspot",
            )

        # Obtain access token
        await self._refresh_token()

        from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers
        self._client = RetryClient(
            base_url=HIGHSPOT_API,
            headers={**bearer_headers(self._access_token), "Content-Type": "application/json"},
            timeout=30.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify connectivity
        resp = await self._request("GET", "/api/v1/items", params={"limit": "1"})
        if resp.status_code in (401, 403):
            await self._client.aclose()
            self._client = None
            raise ConnectorAuthError(
                "Highspot token invalid after exchange", connector_type="highspot"
            )
        logger.info("Highspot authenticated successfully")

    async def _refresh_token(self) -> None:
        """Obtain or refresh the OAuth access token."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                HIGHSPOT_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if resp.status_code != 200:
            raise ConnectorAuthError(
                f"Highspot token exchange failed ({resp.status_code}): {resp.text}",
                connector_type="highspot",
            )

        data = resp.json()
        self._access_token = data.get("access_token", "")
        if not self._access_token:
            raise ConnectorAuthError(
                "Highspot token response missing access_token", connector_type="highspot"
            )

        # Update client headers if client already exists
        if self._client:
            self._client.headers["Authorization"] = f"Bearer {self._access_token}"

    # ------------------------------------------------------------------
    # list_documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List Highspot items, optionally filtered by modification date."""
        assert self._client is not None
        offset = 0

        while True:
            params: dict[str, str] = {
                "limit": str(self._page_size),
                "offset": str(offset),
            }
            if since:
                params["modified_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

            resp = await self._request("GET", "/api/v1/items", params=params)
            data = resp.json()
            items = data.get("items", data.get("results", []))

            if not items:
                break

            for item in items:
                item_id = item.get("id", "")
                title = item.get("title", item.get("name", "Untitled"))
                modified = _parse_dt(item.get("modified_at", item.get("updated_at", "")))

                content_type = item.get("content_type", "application/octet-stream")
                size = item.get("size") or item.get("file_size")

                yield DocumentMetadata(
                    external_id=f"highspot:item:{item_id}",
                    title=title,
                    url=item.get("url") or item.get("web_url"),
                    content_type=content_type,
                    size_bytes=size,
                    author=item.get("author", {}).get("email") if isinstance(item.get("author"), dict) else item.get("author"),
                    modified_at=modified,
                    metadata={
                        "type": "item",
                        "item_type": item.get("type"),
                        "spot_id": item.get("spot_id"),
                        "source": "highspot",
                    },
                )

            # Check if there are more pages
            total = data.get("total", data.get("count", 0))
            offset += self._page_size
            if offset >= total or len(items) < self._page_size:
                break

    # ------------------------------------------------------------------
    # fetch_document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a Highspot item's content by external ID."""
        assert self._client is not None
        parts = doc_id.split(":")
        if len(parts) < 3 or parts[0] != "highspot":
            raise ValueError(f"Invalid Highspot doc_id format: {doc_id}")

        item_id = parts[2]

        # Fetch item content (raw bytes)
        resp = await self._request("GET", f"/api/v1/items/{item_id}/content")
        content_type = resp.headers.get("Content-Type", "application/octet-stream")

        return RawDocument(
            external_id=doc_id,
            content=resp.content,
            content_type=content_type,
            metadata={"type": "item"},
        )

    # ------------------------------------------------------------------
    # get_permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Highspot does not expose document-level permissions via API."""
        return []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            resp = await self._request("GET", "/api/v1/items", params={"limit": "1"})
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # internal HTTP helper
    # ------------------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an HTTP request with rate-limit, token refresh, and error handling."""
        assert self._client is not None

        for attempt in range(4):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"Highspot request timed out: {exc}", connector_type="highspot"
                ) from exc

            # Token expired — try to refresh once
            if resp.status_code == 401 and attempt == 0:
                logger.info("Highspot token expired, refreshing")
                try:
                    await self._refresh_token()
                except ConnectorAuthError:
                    raise ConnectorAuthError(
                        "Highspot auth failed after token refresh",
                        connector_type="highspot",
                    )
                continue

            if resp.status_code == 401:
                raise ConnectorAuthError(
                    "Highspot auth failed (401)", connector_type="highspot"
                )

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "10"))
                if attempt < 3:
                    logger.warning("Highspot rate limited, sleeping %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                raise ConnectorRateLimitError(
                    "Highspot rate limit exceeded",
                    connector_type="highspot",
                    retry_after=retry_after,
                )

            if resp.status_code >= 500:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"Highspot server error {resp.status_code}", connector_type="highspot"
                )

            resp.raise_for_status()
            return resp

        raise ConnectorTransientError(
            "Highspot max retries exceeded", connector_type="highspot"
        )


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime from Highspot API."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
