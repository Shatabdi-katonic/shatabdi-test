"""HubSpot connector.

API: HubSpot CRM v3 + CMS v3
Auth: Bearer token (private app token)
Sync: Incremental (hs_lastmodifieddate filter) + full
Permissions: Not supported (HubSpot uses org-level access)

Content types indexed:
  - Contacts (CRM)
  - Deals (CRM)
  - Blog posts (CMS)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers
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

HUBSPOT_BASE = "https://api.hubapi.com"


class HubSpotConnector(ConnectorBase):
    """HubSpot connector for contacts, deals, and blog posts."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._index_contacts: bool = config.get("index_contacts", True)
        self._index_deals: bool = config.get("index_deals", True)
        self._index_blogs: bool = config.get("index_blog_posts", True)
        # HubSpot authenticates with a single Private App access token (Bearer
        # ``pat-…``) — NOT an OAuth2 client_id/secret code flow. Capture the
        # AddKnowledgeModal form key (``hubspotApiKey``) alongside the canonical
        # keys so an api_key-style source works. Same field-mapping fallback as
        # Trello (CR-553) / Outline / ClickUp.
        self._config_token: str = (
            config.get("hubspotApiKey")
            or config.get("private_app_token")
            or config.get("access_token")
            or config.get("api_key")
            or ""
        )
        self._client: httpx.AsyncClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with a HubSpot Private App access token (Bearer ``pat-…``)."""
        token = (
            credentials.get("access_token", "")
            or credentials.get("private_app_token", "")
            or credentials.get("api_key", "")
            or self._config_token
        )
        if not token:
            raise ConnectorAuthError(
                "HubSpot connector requires a Private App access token "
                "(config 'hubspotApiKey' / 'private_app_token')",
                connector_type="hubspot",
            )

        self._client = RetryClient(
            base_url=HUBSPOT_BASE,
            headers={**bearer_headers(token), "Content-Type": "application/json"},
            timeout=30.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify the token. Use the account-details endpoint (reachable by any
        # Private App token) instead of the api-usage endpoint, which returns
        # 404 for some accounts/scopes — and because `_request` calls
        # `raise_for_status()`, that 404 raised an HTTPStatusError that aborted
        # the ENTIRE sync at discovery, before any document was listed
        # (observed live: "Client error '404 Not Found' for
        # /account-info/v3/api-usage/daily/private-app"). Only a genuine auth
        # failure (401, surfaced by `_request` as ConnectorAuthError) is fatal;
        # any other probe error (404 / 403 scope / 5xx) is logged and tolerated
        # — the real auth check then happens on the first document fetch in
        # `list_documents`, which raises ConnectorAuthError on a 401.
        try:
            await self._request("GET", "/account-info/v3/details")
            logger.info("HubSpot authenticated successfully")
        except ConnectorAuthError:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(
                "Invalid HubSpot private app token", connector_type="hubspot"
            )
        except Exception as exc:  # noqa: BLE001 — probe is best-effort
            logger.warning(
                "HubSpot auth probe (/account-info/v3/details) returned a "
                "non-auth error (%s); proceeding — document fetch will surface "
                "any real auth/scope failure.",
                exc,
            )

    # ------------------------------------------------------------------
    # list_documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List contacts, deals, and blog posts, optionally filtered by modification date."""
        assert self._client is not None

        if self._index_contacts:
            async for doc in self._list_crm_objects("contacts", since):
                yield doc

        if self._index_deals:
            async for doc in self._list_crm_objects("deals", since):
                yield doc

        if self._index_blogs:
            async for doc in self._list_blog_posts(since):
                yield doc

    async def _list_crm_objects(
        self, object_type: str, since: datetime | None
    ) -> AsyncIterator[DocumentMetadata]:
        """Paginate through CRM objects (contacts or deals)."""
        after: str | None = None

        while True:
            params: dict[str, str] = {"limit": "100"}
            if after:
                params["after"] = after

            resp = await self._request("GET", f"/crm/v3/objects/{object_type}", params=params)
            data = resp.json()

            for obj in data.get("results", []):
                props = obj.get("properties", {})
                modified_str = props.get("hs_lastmodifieddate", "")
                modified = _parse_dt(modified_str)

                if since and modified < since:
                    continue

                title = _build_crm_title(object_type, props, obj.get("id", ""))
                yield DocumentMetadata(
                    external_id=f"hubspot:{object_type}:{obj['id']}",
                    title=title,
                    url=f"https://app.hubspot.com/{object_type}/{obj['id']}",
                    content_type="application/json",
                    author=props.get("hubspot_owner_id"),
                    modified_at=modified,
                    metadata={"type": object_type, "source": "hubspot"},
                )

            paging = data.get("paging", {}).get("next", {})
            after = paging.get("after")
            if not after:
                break

    async def _list_blog_posts(self, since: datetime | None) -> AsyncIterator[DocumentMetadata]:
        """Paginate through CMS blog posts."""
        offset = 0
        limit = 100

        while True:
            params: dict[str, str] = {"limit": str(limit), "offset": str(offset)}
            resp = await self._request("GET", "/cms/v3/blogs/posts", params=params)
            data = resp.json()

            for post in data.get("results", []):
                modified = _parse_dt(post.get("updated", ""))
                if since and modified < since:
                    continue

                yield DocumentMetadata(
                    external_id=f"hubspot:blog:{post['id']}",
                    title=post.get("name", post.get("slug", "Untitled")),
                    url=post.get("url"),
                    content_type="text/html",
                    author=post.get("authorName"),
                    modified_at=modified,
                    metadata={"type": "blog_post", "state": post.get("state"), "source": "hubspot"},
                )

            total = data.get("total", 0)
            offset += limit
            if offset >= total or not data.get("results"):
                break

    # ------------------------------------------------------------------
    # fetch_document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single HubSpot object by external ID."""
        assert self._client is not None
        parts = doc_id.split(":")
        if len(parts) < 3 or parts[0] != "hubspot":
            raise ValueError(f"Invalid HubSpot doc_id format: {doc_id}")

        obj_type = parts[1]
        obj_id = parts[2]

        if obj_type == "blog":
            return await self._fetch_blog_post(obj_id)

        # CRM objects (contacts, deals)
        resp = await self._request(
            "GET",
            f"/crm/v3/objects/{obj_type}/{obj_id}",
            params={"properties": ",".join(_default_properties(obj_type))},
        )
        data = resp.json()
        props = data.get("properties", {})

        lines = [f"# {obj_type.title()} {obj_id}", ""]
        for key, value in sorted(props.items()):
            if value:
                lines.append(f"**{key}:** {value}")

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=doc_id,
            content=content,
            content_type="text/plain",
            metadata={"type": obj_type},
        )

    async def _fetch_blog_post(self, post_id: str) -> RawDocument:
        """Fetch a CMS blog post by ID."""
        resp = await self._request("GET", f"/cms/v3/blogs/posts/{post_id}")
        post = resp.json()

        title = post.get("name", "Untitled")
        body = post.get("postBody", post.get("postSummary", ""))
        content = f"# {title}\n\n{body}".encode("utf-8")

        return RawDocument(
            external_id=f"hubspot:blog:{post_id}",
            content=content,
            content_type="text/html",
            metadata={"filename": f"{post.get('slug', post_id)}.html"},
        )

    # ------------------------------------------------------------------
    # get_permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """HubSpot does not expose document-level permissions."""
        return []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            resp = await self._request("GET", "/crm/v3/objects/contacts", params={"limit": "1"})
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    # ------------------------------------------------------------------
    # internal HTTP helper
    # ------------------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an HTTP request with rate-limit and error handling."""
        assert self._client is not None

        for attempt in range(4):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"HubSpot request timed out: {exc}", connector_type="hubspot"
                ) from exc

            if resp.status_code == 401:
                raise ConnectorAuthError("HubSpot auth failed (401)", connector_type="hubspot")

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "10"))
                if attempt < 3:
                    logger.warning("HubSpot rate limited, sleeping %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                raise ConnectorRateLimitError(
                    "HubSpot rate limit exceeded",
                    connector_type="hubspot",
                    retry_after=retry_after,
                )

            if resp.status_code >= 500:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"HubSpot server error {resp.status_code}", connector_type="hubspot"
                )

            resp.raise_for_status()
            return resp

        raise ConnectorTransientError("HubSpot max retries exceeded", connector_type="hubspot")


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime from HubSpot API."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _build_crm_title(object_type: str, props: dict, obj_id: str) -> str:
    """Build a human-readable title for a CRM object."""
    if object_type == "contacts":
        first = props.get("firstname", "")
        last = props.get("lastname", "")
        email = props.get("email", "")
        name = f"{first} {last}".strip()
        return name or email or f"Contact {obj_id}"
    if object_type == "deals":
        return props.get("dealname", f"Deal {obj_id}")
    return f"{object_type} {obj_id}"


def _default_properties(object_type: str) -> list[str]:
    """Return sensible default properties to fetch for each CRM object type."""
    if object_type == "contacts":
        return [
            "firstname", "lastname", "email", "phone", "company",
            "jobtitle", "lifecyclestage", "hubspot_owner_id",
            "hs_lastmodifieddate", "createdate",
        ]
    if object_type == "deals":
        return [
            "dealname", "dealstage", "amount", "pipeline",
            "closedate", "hubspot_owner_id", "hs_lastmodifieddate", "createdate",
        ]
    return []
