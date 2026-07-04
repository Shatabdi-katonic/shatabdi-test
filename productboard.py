"""Productboard connector.

API: Productboard Public API
Auth: Bearer api_token
Sync: Incremental (updatedAt filter) with cursor pagination (pageCursor/pageLimit)
Permissions: Not supported (returns empty)
"""

from __future__ import annotations

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

_API_BASE = "https://api.productboard.com"


class ProductBoardConnector(ConnectorBase):
    """Native Productboard connector for features.

    Config:
        (no additional config required beyond credentials)
    """

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using a Bearer API token."""
        token = credentials.get("api_token") or credentials.get("api_key", "")
        if not token:
            raise ConnectorAuthError(
                "Productboard requires 'api_token' credential",
                connector_type="productboard",
            )

        headers = bearer_headers(token)
        self._client = RetryClient(base_url=_API_BASE, headers=headers, rate_limiter=self.rate_limiter)

        # Verify access by fetching first page of features
        try:
            await self._client.get_json("/features", params={"pageLimit": "1"})
            logger.info("Productboard authenticated successfully")
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(
                f"Productboard authentication failed: {exc}",
                connector_type="productboard",
            ) from exc

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List Productboard features with cursor pagination.

        Filters by updatedAt when ``since`` is provided.
        """
        assert self._client is not None

        cursor: str | None = None

        while True:
            params: dict[str, str] = {"pageLimit": "100"}
            if cursor:
                params["pageCursor"] = cursor
            if since:
                params["updatedAt[gte]"] = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            try:
                data = await self._client.get_json("/features", params=params)
            except Exception as exc:
                _raise_mapped(exc, "productboard")
                raise

            features = data.get("data", [])
            if not features:
                break

            for feat in features:
                updated = _parse_ts(feat.get("updatedAt", ""))
                status = feat.get("status", {})

                yield DocumentMetadata(
                    external_id=feat["id"],
                    title=feat.get("name", ""),
                    url=feat.get("links", {}).get("html"),
                    content_type="text/plain",
                    modified_at=updated,
                    metadata={
                        "status": status.get("name") if isinstance(status, dict) else str(status),
                        "type": feat.get("type"),
                    },
                )

            # Cursor pagination
            pagination = data.get("pageCursor") or data.get("links", {}).get("next")
            if isinstance(pagination, str) and pagination:
                cursor = pagination
            elif isinstance(data.get("links"), dict):
                next_link = data["links"].get("next")
                if next_link:
                    # Extract cursor from next link if present
                    cursor = _extract_cursor(next_link)
                    if not cursor:
                        break  # Cursor extraction failed — stop to avoid infinite loop
                else:
                    break
            else:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single Productboard feature."""
        assert self._client is not None

        try:
            data = await self._client.get_json(f"/features/{doc_id}")
        except Exception as exc:
            _raise_mapped(exc, "productboard")
            raise

        feat = data.get("data", data)
        parts: list[str] = []

        parts.append(f"# {feat.get('name', '')}")
        parts.append("")

        status = feat.get("status", {})
        status_name = status.get("name") if isinstance(status, dict) else str(status)
        if status_name:
            parts.append(f"**Status:** {status_name}")

        feat_type = feat.get("type")
        if feat_type:
            parts.append(f"**Type:** {feat_type}")

        if feat.get("timeframe"):
            parts.append(f"**Timeframe:** {feat['timeframe']}")

        parent = feat.get("parent")
        if parent and isinstance(parent, dict):
            parts.append(f"**Parent:** {parent.get('name', parent.get('id', ''))}")

        parts.append("")

        description = feat.get("description", "")
        if description:
            parts.append(description)
            parts.append("")

        # Notes field if present
        notes = feat.get("notes")
        if notes:
            parts.append("## Notes")
            parts.append(notes)

        content = "\n".join(parts)
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={"title": feat.get("name", "")},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Productboard does not expose feature-level permissions."""
        return []

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/features", params={"pageLimit": "1"})
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


def _extract_cursor(next_link: str) -> str | None:
    """Extract pageCursor value from a next-page URL."""
    import logging
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(next_link)
        qs = parse_qs(parsed.query)
        cursors = qs.get("pageCursor", [])
        if not cursors:
            logging.getLogger(__name__).warning(
                "ProductBoard: no pageCursor in next link %s — pagination may be incomplete", next_link
            )
        return cursors[0] if cursors else None
    except Exception as e:
        logging.getLogger(__name__).warning("ProductBoard: failed to extract cursor from %s: %s", next_link, e)
        return None


def _raise_mapped(exc: Exception, connector_type: str) -> None:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            raise ConnectorAuthError(str(exc), connector_type=connector_type) from exc
        if code == 429:
            retry_after = float(exc.response.headers.get("Retry-After", "10"))
            raise ConnectorRateLimitError(
                str(exc), connector_type=connector_type, retry_after=retry_after
            ) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
