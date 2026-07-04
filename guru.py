"""Guru connector.

API: Guru REST API v1
Auth: Basic auth (email + token, base64-encoded)
Sync: Incremental (lastModified filter) + full
Permissions: Collection-level via group membership + card owner

Permission model:
  - Cards belong to Collections.
  - Groups are assigned to Collections with roles.
  - Groups contain members (users with emails).
  - Card owner is mapped as ``owner``; group members with collection
    access are mapped as ``viewer`` (inherited).

Content types indexed:
  - Cards (HTML content)
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

GURU_API = "https://api.getguru.com"
PAGE_SIZE = 100


class GuruConnector(ConnectorBase):
    """Guru knowledge-base connector for cards."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._collection_id: str | None = config.get("collection_id")
        # Hold onto possible config-side email + token so
        # authenticate() can fall back when the sync route hasn't
        # materialised them into credentials. API-key Guru sources
        # created via AddKnowledgeModal carry these in config.
        self._fallback_email: str = config.get("guruEmail") or ""
        self._fallback_token: str = config.get("guruToken") or ""
        self._client: RetryClient | None = None
        # Cache: collection_id → list[PermissionEntry] (group-based viewers)
        self._collection_perms_cache: dict[str, list[PermissionEntry]] = {}
        # Cache: groups list fetched from /api/v1/groups (populated once)
        self._groups_cache: list[dict] | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using email + token via HTTP Basic auth.

        Expects credentials: {email: str, token: str}
        """
        # Prefer canonical credentials path. Fall back to values
        # captured from config in __init__ — that's how API-key Guru
        # sources created via AddKnowledgeModal carry email + token.
        email = credentials.get("email") or self._fallback_email or ""
        token = (
            credentials.get("token")
            or credentials.get("api_token")
            or self._fallback_token
            or ""
        )
        if not email or not token:
            raise ConnectorAuthError(
                "Guru requires email and token",
                connector_type="guru",
            )

        # Guru uses email:token as Basic auth
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()

        self._client = RetryClient(
            base_url=GURU_API,
            headers={
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        # Verify connectivity
        try:
            await self._client.get_json("/api/v1/members/me")
            logger.info("Guru authenticated for %s", email)
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"Guru authentication failed: {e}",
                connector_type="guru",
            ) from e

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all cards, paginated. Filter by lastModified > since."""
        assert self._client is not None

        # Guru uses header-based pagination via Link or query params
        page = 1

        while True:
            params: dict = {"maxResults": PAGE_SIZE, "page": page}
            if self._collection_id:
                params["collectionId"] = self._collection_id

            try:
                resp = await self._client.get("/api/v1/cards", params=params)
            except Exception as e:
                _raise_typed(e, "guru")

            cards = resp.json()

            # Guru returns an empty list when no more results
            if not cards or not isinstance(cards, list):
                break

            for card in cards:
                last_modified = _parse_dt(card.get("lastModified", ""))
                if since and last_modified <= since:
                    continue

                card_id = card.get("id", "")
                slug = card.get("slug", "")
                collection = card.get("collection", {})

                yield DocumentMetadata(
                    external_id=card_id,
                    title=card.get("preferredPhrase", "Untitled"),
                    url=f"https://app.getguru.com/card/{slug}" if slug else None,
                    content_type="text/html",
                    author=card.get("owner", {}).get("email"),
                    modified_at=last_modified,
                    metadata={
                        "collection_id": collection.get("id"),
                        "collection_name": collection.get("name"),
                        "verification_state": card.get("verificationState"),
                    },
                )

            if len(cards) < PAGE_SIZE:
                break
            page += 1

    # ------------------------------------------------------------------
    # Fetch document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single card by ID. Returns HTML content."""
        assert self._client is not None

        try:
            data = await self._client.get_json(f"/api/v1/cards/{doc_id}")
        except Exception as e:
            _raise_typed(e, "guru")

        html = data.get("content", "")
        title = data.get("preferredPhrase", "Untitled")

        return RawDocument(
            external_id=doc_id,
            content=html.encode("utf-8"),
            content_type="text/html",
            metadata={"title": title},
        )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Return permission entries for a Guru card.

        Strategy:
          1. Fetch the card to obtain its owner email and collection id.
          2. Map the card owner as an ``owner`` permission entry.
          3. Resolve collection-level group memberships (cached) and map
             every group member who has access to the card's collection
             as a ``viewer`` (inherited).
        """
        assert self._client is not None

        # 1. Fetch card details (owner + collection)
        try:
            card = await self._client.get_json(f"/api/v1/cards/{doc_id}")
        except Exception as e:
            logger.warning("Failed to fetch card %s for permissions: %s", doc_id, e)
            return []

        entries: list[PermissionEntry] = []

        # 2. Card owner → owner
        owner = card.get("owner") or {}
        owner_email = owner.get("email", "")
        if owner_email:
            entries.append(
                PermissionEntry(
                    subject_type="user",
                    subject_id=owner_email,
                    relation="owner",
                )
            )

        # 3. Collection-level group members → viewer (inherited)
        collection_id = (card.get("collection") or {}).get("id")
        if collection_id:
            # Add a group-level entry for the collection itself so that
            # SpiceDB can resolve collection-wide access in one hop.
            entries.append(
                PermissionEntry(
                    subject_type="group",
                    subject_id=f"guru_collection:{collection_id}",
                    relation="viewer",
                    inherited=True,
                )
            )

            if collection_id not in self._collection_perms_cache:
                self._collection_perms_cache[collection_id] = (
                    await self._fetch_collection_group_members(collection_id)
                )
            entries.extend(self._collection_perms_cache[collection_id])

        return entries

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    async def _ensure_groups_cache(self) -> list[dict]:
        """Fetch and cache the full list of Guru groups (once per sync)."""
        if self._groups_cache is not None:
            return self._groups_cache

        assert self._client is not None
        try:
            groups = await self._client.get_json("/api/v1/groups")
            self._groups_cache = groups if isinstance(groups, list) else []
        except Exception as e:
            logger.warning("Failed to fetch Guru groups: %s", e)
            self._groups_cache = []

        return self._groups_cache

    async def _fetch_collection_group_members(
        self, collection_id: str
    ) -> list[PermissionEntry]:
        """Return viewer PermissionEntries for every user in groups
        that have access to *collection_id*.

        Approach:
          - For each group, call ``GET /api/v1/groups/{groupId}/members``
            to get member emails.
          - Check ``GET /api/v1/collections/{collectionId}`` for the
            ``rolesInCollection`` field or fall back to iterating groups.
          - Guru's ``GET /api/v1/groups`` response may include a nested
            ``collections`` key; if a group lists the target collection,
            its members are granted viewer access.
          - As a fallback (enterprise/free tier where all groups access
            all collections), we check each group's collection membership
            by querying the group-specific collection endpoint.
        """
        assert self._client is not None
        groups = await self._ensure_groups_cache()

        entries: list[PermissionEntry] = []
        seen_emails: set[str] = set()

        for group in groups:
            group_id = group.get("id", "")
            if not group_id:
                continue

            # Check if this group has access to the target collection.
            # If the group object carries its collection list, check directly.
            # If not (non-enterprise tier), try the collection membership API.
            group_collections = group.get("collections") or []
            if group_collections:
                coll_ids = {c.get("id") for c in group_collections if isinstance(c, dict)}
                if collection_id not in coll_ids:
                    continue
            else:
                # No collections field — try explicit API check before assuming access
                try:
                    group_colls = await self._client.get_json(
                        f"/api/v1/groups/{group_id}/collections"
                    )
                    if isinstance(group_colls, list):
                        coll_ids = {c.get("id") for c in group_colls if isinstance(c, dict)}
                        if collection_id not in coll_ids:
                            continue
                except Exception:
                    # API not available (free tier) — assume access with warning
                    logger.warning(
                        "Guru: cannot verify group %s access to collection %s "
                        "(API unavailable, assuming access — may over-grant permissions)",
                        group_id, collection_id,
                    )

            # Fetch group members
            try:
                members_data = await self._client.get_json(
                    f"/api/v1/groups/{group_id}/members"
                )
            except Exception as e:
                logger.debug(
                    "Could not fetch members for group %s: %s", group_id, e
                )
                continue

            if not isinstance(members_data, list):
                continue

            for member in members_data:
                # Members may be nested under a "user" key or flat.
                email = (
                    member.get("user", {}).get("email")
                    or member.get("email", "")
                )
                if email and email not in seen_emails:
                    seen_emails.add(email)
                    entries.append(
                        PermissionEntry(
                            subject_type="user",
                            subject_id=email,
                            relation="viewer",
                            inherited=True,
                        )
                    )

        return entries

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/api/v1/members/me")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_dt(s: str) -> datetime:
    """Parse Guru datetime string (ISO format)."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _raise_typed(exc: Exception, connector_type: str) -> None:
    """Re-raise as the appropriate ConnectorError subclass."""
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
    raise exc