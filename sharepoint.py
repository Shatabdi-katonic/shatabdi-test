"""SharePoint / OneDrive connector.

API: Microsoft Graph API v1.0
Auth: OAuth 2.0 (Azure AD application)
Sync: Incremental (delta query) + full
Permissions: Graph permissions API + Azure AD group resolution

Role mapping (spec section 15.2):
  Full Control, Design              -> editor
  Edit, Contribute                  -> editor
  Read, View Only, Limited Access   -> viewer
  External sharing                  -> BLOCKED (flagged for admin)

Supports:
- SharePoint sites/document libraries
- OneDrive (personal and business)
- Shared drives
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import (
    RetryClient,
    bearer_headers,
    paginate,
)
from platform_knowledge_engine.connectors.base import (
    ConnectorAuthError,
    ConnectorBase,
    ConnectorTransientError,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.microsoft.com/v1.0"

# SharePoint permission roles -> our relations
ROLE_MAP = {
    "owner": "editor",
    "write": "editor",
    "read": "viewer",
    "sp.full control": "editor",
    "sp.design": "editor",
    "sp.edit": "editor",
    "sp.contribute": "editor",
    "sp.read": "viewer",
    "sp.view only": "viewer",
    "sp.limited access": "viewer",
}


class SharePointConnector(ConnectorBase):
    """Native SharePoint/OneDrive connector via Microsoft Graph.

    Config:
        site_urls: List of SharePoint site URLs to index.
        drive_ids: List of specific drive IDs (overrides site_urls).
        include_onedrive: Whether to include the user's OneDrive.
        block_external_sharing: Block externally shared items (default True).
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._config = config
        site_urls = config.get("site_urls", [])
        if not site_urls:
            # Frontend sends SharePoint site URL(s) as "sharepointUrl" — a
            # single URL or a comma/newline-separated list, since a tenant
            # can have many sites (SharePoint sites are like separate
            # "drives" — see CR-612). Mirrors how confluenceSpaceKey is
            # parsed for the Confluence connector.
            sp_url_raw = config.get("sharepointUrl", "")
            if sp_url_raw:
                site_urls = [
                    u.strip()
                    for u in sp_url_raw.replace("\n", ",").split(",")
                    if u.strip()
                ]
        self._site_urls: list[str] = site_urls
        self._drive_ids: list[str] = config.get("drive_ids", [])
        self._include_onedrive: bool = config.get("include_onedrive", False)
        self._block_external: bool = config.get("block_external_sharing", True)
        self._client: RetryClient | None = None
        self._resolved_drives: list[str] = []

    async def _acquire_token_client_credentials(self) -> str:
        """Obtain a Graph API token using Azure AD client_credentials flow."""
        import httpx

        tenant_id = self._config.get("sharepointTenantId", "")
        client_id = self._config.get("sharepointClientId", "")
        client_secret = self._config.get("sharepointClientSecret", "")
        if not (tenant_id and client_id and client_secret):
            raise ConnectorAuthError(
                "SharePoint client_credentials requires sharepointTenantId, "
                "sharepointClientId, and sharepointClientSecret in config",
                connector_type="sharepoint",
            )
        url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        token = data.get("access_token", "")
        if not token:
            raise ConnectorAuthError(
                "Azure AD token response missing access_token",
                connector_type="sharepoint",
            )
        return token

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")

        # If no pre-obtained token, try client_credentials flow from config
        if not token and self._config.get("sharepointClientId"):
            token = await self._acquire_token_client_credentials()

        if not token:
            raise ConnectorAuthError(
                "SharePoint connector requires access_token or client credentials in config",
                connector_type="sharepoint",
            )
        self._client = RetryClient(
            base_url=GRAPH_API,
            headers=bearer_headers(token),
            rate_limiter=self.rate_limiter,
        )
        # NOTE: drive resolution is intentionally NOT done here. Resolving
        # drives can be expensive — when no site_urls/drive_ids are
        # configured it crawls every site in the tenant via /sites?search=*
        # and lists each site's drives. Only list_documents() (discovery)
        # needs the resolved-drive list; fetch_document(), get_permissions()
        # and health_check() all derive drive_id from the document's own
        # external_id ("{drive_id}:{item_id}"). Because the Temporal worker
        # re-instantiates and re-authenticates a fresh connector for EVERY
        # per-document fetch and permission sync, resolving drives in
        # authenticate() made each document pay a full (sometimes
        # tenant-wide) drive crawl — the cause of SharePoint syncs sitting
        # in "pending" with 0 chunks for a very long time. Resolution is now
        # lazy: list_documents() resolves once on first use.
        logger.info("SharePoint authenticated (drive resolution deferred to discovery)")

    # SharePoint sites ship hidden system document libraries alongside the
    # real content library. Their files (theme XML, wiki/page images,
    # thumbnails, form templates, style assets, preservation copies) are not
    # knowledge content and massively inflate the document count. Match by the
    # library's display name, case-insensitively. Users who genuinely need one
    # of these can target it explicitly via the `drive_ids` config, which
    # bypasses this filter.
    _SYSTEM_LIBRARY_NAMES: frozenset[str] = frozenset(
        {
            "site assets",
            "style library",
            "form templates",
            "site pages",
            "preservation hold library",
            "teams wiki data",
            "site collection documents",
            "site collection images",
            "images",
            "theme",
            "themes",
            "_catalogs",
            "converted forms",
        }
    )

    def _is_content_drive(self, drive: dict) -> bool:
        """True if a drive is a real content library worth indexing.

        Skips SharePoint's hidden/system document libraries (Site Assets,
        Style Library, theme store, Form Templates, etc.) that would
        otherwise flood the source with theme XML and image assets.
        """
        # Only document libraries hold files; ignore other drive types.
        drive_type = (drive.get("driveType") or "").lower()
        if drive_type and drive_type != "documentlibrary":
            return False
        name = (drive.get("name") or "").strip().lower()
        return name not in self._SYSTEM_LIBRARY_NAMES

    async def _resolve_drives(self) -> None:
        """Resolve site URLs and config into drive IDs."""
        assert self._client is not None
        self._resolved_drives = list(self._drive_ids)

        for site_url in self._site_urls:
            try:
                # Extract hostname and path from URL
                # e.g., https://contoso.sharepoint.com/sites/marketing
                from urllib.parse import urlparse

                parsed = urlparse(site_url)
                hostname = parsed.hostname or ""
                path = parsed.path.rstrip("/")

                site = await self._client.get_json(f"/sites/{hostname}:{path}")
                site_id = site["id"]

                # Get document libraries for this site, skipping SharePoint's
                # hidden system libraries (see _is_content_drive). Without this
                # the connector indexes Site Assets (dashboard.png / thumbnails),
                # the theme store (hundreds of TSQ/PSQ/Minimal *.xml files),
                # Style Library, Form Templates, etc. — none of which the user
                # sees in the Documents view, but which inflate the document
                # count into the tens of thousands and bury real content.
                async for drive in paginate(
                    self._client,
                    f"/sites/{site_id}/drives",
                    items_key="value",
                    next_key="@odata.nextLink",
                ):
                    if self._is_content_drive(drive):
                        self._resolved_drives.append(drive["id"])
                    else:
                        logger.info(
                            "SharePoint: skipping system library %r (driveType=%s)",
                            drive.get("name"),
                            drive.get("driveType"),
                        )

            except Exception as e:
                logger.error("Failed to resolve SharePoint site %s: %s", site_url, e)
        # No explicit site_urls / drive_ids configured — a tenant can have
        # many SharePoint sites (each is effectively its own "drive"; see
        # CR-612), and previously the connector silently indexed nothing
        # in this case while still reporting a healthy "Synced" status.
        # Mirror the Confluence connector's behavior (sync all accessible
        # spaces when none are specified) by discovering every site the
        # connected account/app can see via Graph and resolving drives for
        # all of them.
        if not self._site_urls and not self._drive_ids:
            try:
                seen_site_ids: set[str] = set()
                async for site_id in self._discover_all_site_ids():
                    # /sites?search=* is a documented Graph quirk: it can
                    # return the same site more than once across result
                    # pages. Without dedup, that site's drives — and every
                    # file in them — get listed and processed twice.
                    if site_id in seen_site_ids:
                        continue
                    seen_site_ids.add(site_id)
                    try:
                        async for drive in paginate(
                            self._client,
                            f"/sites/{site_id}/drives",
                            items_key="value",
                            next_key="@odata.nextLink",
                        ):
                            if self._is_content_drive(drive):
                                self._resolved_drives.append(drive["id"])
                    except Exception as e:
                        logger.warning(
                            "Failed to resolve drives for discovered site %s: %s", site_id, e
                        )
            except Exception as e:
                logger.error("SharePoint site auto-discovery failed: %s", e)

        if self._include_onedrive:
            try:
                me_drive = await self._client.get_json("/me/drive")
                self._resolved_drives.append(me_drive["id"])
            except Exception as e:
                logger.warning("Failed to resolve OneDrive: %s", e)

        # Final safety net: dedupe the resolved drive list itself. A drive
        # can legitimately be reachable via more than one path (e.g.
        # explicit site_urls overlapping with auto-discovery results, or
        # the same drive surfaced under two site aliases), and duplicate
        # drive IDs here mean duplicate document listing/processing
        # downstream. dict.fromkeys preserves resolution order.
        self._resolved_drives = list(dict.fromkeys(self._resolved_drives))
    async def _discover_all_site_ids(self) -> AsyncIterator[str]:
        """Enumerate every SharePoint site visible to the connected
        account/app via Graph's site search endpoint, used as a fallback
        when no specific sites were configured.

        `/sites?search=*` is the documented Graph approach for listing
        all sites in a tenant (there's no dedicated "list all sites"
        endpoint); it's paginated like any other Graph collection.
        """
        assert self._client is not None
        async for site in paginate(
            self._client,
            "/sites",
            items_key="value",
            next_key="@odata.nextLink",
            params={"search": "*"},
        ):
            site_id = site.get("id")
            if site_id:
                yield site_id

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None

        if not self._resolved_drives:
            await self._resolve_drives()

        # Dedupe drives before enumerating. A drive can legitimately be
        # reachable via more than one path (explicit site_urls overlapping
        # with auto-discovery, or the same library surfaced under two site
        # aliases). Walking the same drive_id twice lists every file twice —
        # the direct cause of a document count exploding far past the real
        # number of files in the site. dict.fromkeys preserves order.
        self._resolved_drives = list(dict.fromkeys(self._resolved_drives))
        logger.info(
            "SharePoint discovery resolved %d unique drive(s)",
            len(self._resolved_drives),
        )

        # Guard against the same item being emitted more than once within a
        # single discovery pass (delta can repeat items across pages, and a
        # file shared into multiple drives can surface more than once). Every
        # duplicate would otherwise create a redundant document row.
        seen_external_ids: set[str] = set()

        for drive_id in self._resolved_drives:
            try:
                async for item in self._list_drive_items(drive_id, since):
                    if item.external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(item.external_id)
                    yield item
            except Exception as e:
                logger.error("Error listing drive %s: %s", drive_id, e)
                raise ConnectorTransientError(
                    f"Error listing drive {drive_id}: {e}",
                    connector_type="sharepoint",
                ) from e

    async def _list_drive_items(
        self,
        drive_id: str,
        since: datetime | None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List file items in a drive using delta query for incremental sync.

        Uses the Microsoft Graph delta endpoint when a delta link is available,
        which returns only changed items since the last sync. Falls back to
        full search for the initial sync.
        """
        assert self._client is not None

        # Try delta query for efficient incremental sync
        delta_key = f"delta_link:{drive_id}"
        delta_link = self._config.get(delta_key)
        if since and delta_link:
            async for doc in self._list_via_delta(drive_id, delta_link):
                yield doc
            return

        # Full listing via the drive `delta` endpoint (initial sync or no
        # stored delta link).
        #
        # History of this method:
        #   1. `/root/search(q='')` — empty-query search. Could return a
        #      non-terminating @odata.nextLink chain, so discovery hung
        #      forever (docs stuck "pending", 0 chunks, no docs_found, no error).
        #   2. recursive `/children` walk — terminates, but issues ONE Graph
        #      request per folder. On a real SharePoint library (hundreds /
        #      thousands of folders across multiple drives) that is hundreds of
        #      throttled round-trips, so `list_documents` never finishes inside
        #      the sync window and the 30-min stale-reset restarts it from zero
        #      — again: docs discovered but never processed, 0 chunks. This was
        #      SharePoint-specific; Confluence enumerates in ~1-2 calls and
        #      completes, which is why it chunked fine on the same inline path.
        #
        # The drive `delta` endpoint returns the ENTIRE drive hierarchy in
        # ~200-item pages — a handful of requests for the whole library — and
        # terminates with an @odata.deltaLink. This makes SharePoint discovery
        # return quickly (like Confluence), so the existing inline/Temporal
        # processing can chunk the documents. We yield ONLY real file items
        # (no folders, no deletions, and crucially no sync-state marker), so
        # the inline ingest path persists only genuine documents. Output shape
        # and external_id format are unchanged.
        select = (
            "id,name,file,folder,size,webUrl,"
            "lastModifiedDateTime,lastModifiedBy,parentReference,deleted"
        )
        delta_key = f"delta_link:{drive_id}"
        url: str | None = f"/drives/{drive_id}/root/delta"
        params: dict = {"$select": select}

        while url:
            if url.startswith("http"):
                # nextLink / deltaLink are absolute and carry their own params.
                resp = await self._client.get(url)
                data = resp.json()
            else:
                data = await self._client.get_json(url, params=params)
                params = {}

            for item in data.get("value", []):
                if item.get("deleted"):
                    continue
                # Skip folders and any non-file facet (root, package, etc).
                if "folder" in item or "file" not in item:
                    continue

                modified_at = _parse_timestamp(item.get("lastModifiedDateTime", ""))
                if since and modified_at < since:
                    continue

                author = item.get("lastModifiedBy", {}).get("user", {}).get("email")
                parent = item.get("parentReference", {})

                yield DocumentMetadata(
                    external_id=f"{drive_id}:{item['id']}",
                    title=item.get("name", "Untitled"),
                    url=item.get("webUrl"),
                    content_type=item.get("file", {}).get("mimeType", "application/octet-stream"),
                    size_bytes=item.get("size"),
                    author=author,
                    modified_at=modified_at,
                    folder_id=parent.get("id"),
                    metadata={
                        "drive_id": drive_id,
                        "item_id": item["id"],
                        "site_id": parent.get("siteId"),
                    },
                )

            next_link = data.get("@odata.nextLink")
            if next_link:
                url = next_link
            else:
                # Stash the delta link so future incremental syncs are cheap.
                # Best-effort: self._config is per-instance, so this only helps
                # within a live connector; the Temporal path persists it via
                # _list_via_delta's sync-state marker on subsequent runs.
                delta_final = data.get("@odata.deltaLink")
                if delta_final:
                    self._config[delta_key] = delta_final
                break

    async def _list_via_delta(
        self, drive_id: str, delta_link: str
    ) -> AsyncIterator[DocumentMetadata]:
        """Use Microsoft Graph delta query for efficient incremental sync.

        Returns only items that changed since the last delta link.
        ~10x fewer API calls than full search on large drives.
        """
        url = delta_link
        select_fields = "id,name,file,size,webUrl,lastModifiedDateTime,lastModifiedBy,parentReference,deleted"

        while url:
            # Delta links are absolute URLs with embedded query params — use directly
            if url.startswith("http"):
                resp = await self._client.get(url)
                data = resp.json()
            else:
                # Relative URL — add $select if not already present
                params = {"$select": select_fields} if "?" not in url else {}
                data = await self._client.get_json(url, params=params)

            for item in data.get("value", []):
                # Handle deletions
                if item.get("deleted"):
                    yield DocumentMetadata(
                        external_id=f"{drive_id}:{item['id']}",
                        title="(deleted)",
                        content_type="application/deleted",
                        metadata={"deleted": True},
                    )
                    continue

                if "file" not in item:
                    continue

                modified = _parse_timestamp(item.get("lastModifiedDateTime", ""))
                yield DocumentMetadata(
                    external_id=f"{drive_id}:{item['id']}",
                    title=item.get("name", ""),
                    url=item.get("webUrl"),
                    content_type=item.get("file", {}).get("mimeType", "application/octet-stream"),
                    size_bytes=item.get("size"),
                    author=item.get("lastModifiedBy", {}).get("user", {}).get("email"),
                    modified_at=modified,
                    metadata={"drive_id": drive_id},
                )

            # Store new delta link for next sync
            new_delta = data.get("@odata.deltaLink")
            if new_delta:
                delta_key = f"delta_link:{drive_id}"
                self._config[delta_key] = new_delta
                yield DocumentMetadata(
                    external_id="__delta_link__",
                    title="",
                    content_type="application/x-sync-state",
                    metadata={delta_key: new_delta, "_sync_state": True},
                )

            url = data.get("@odata.nextLink")

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Download file content from drive."""
        assert self._client is not None
        drive_id, item_id = doc_id.split(":", 1)

        resp = await self._client.get(
            f"/drives/{drive_id}/items/{item_id}/content",
        )

        # Get metadata for content type
        meta = await self._client.get_json(
            f"/drives/{drive_id}/items/{item_id}",
            params={"$select": "file,name"},
        )
        content_type = meta.get("file", {}).get("mimeType", "application/octet-stream")

        return RawDocument(
            external_id=doc_id,
            content=resp.content,
            content_type=content_type,
            metadata={"name": meta.get("name", "")},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get permissions via Graph API permissions endpoint.

        Handles:
        - Direct user/group grants
        - SharePoint role assignments
        - Inherited permissions (marked as inherited=True)
        - External sharing (blocked by default)
        """
        assert self._client is not None
        drive_id, item_id = doc_id.split(":", 1)
        entries: list[PermissionEntry] = []

        try:
            data = await self._client.get_json(
                f"/drives/{drive_id}/items/{item_id}/permissions",
            )
        except Exception as e:
            logger.warning("Failed to get permissions for %s: %s", doc_id, e)
            return entries

        for perm in data.get("value", []):
            roles = perm.get("roles", [])
            mapped_role = _map_roles(roles)
            inherited = perm.get("inheritedFrom") is not None

            # Direct user grants
            if "grantedToV2" in perm:
                granted = perm["grantedToV2"]

                if "user" in granted:
                    user = granted["user"]
                    email = user.get("email", "")
                    if email:
                        entries.append(
                            PermissionEntry(
                                subject_type="user",
                                subject_id=email,
                                relation=mapped_role,
                                inherited=inherited,
                            )
                        )

                if "group" in granted:
                    group = granted["group"]
                    group_id = group.get("id", "")
                    if group_id:
                        entries.append(
                            PermissionEntry(
                                subject_type="group",
                                subject_id=group_id,
                                relation=mapped_role,
                                inherited=inherited,
                            )
                        )

            # Sharing links
            if "link" in perm:
                link = perm["link"]
                scope = link.get("scope", "")
                if scope == "organization":
                    entries.append(
                        PermissionEntry(
                            subject_type="domain",
                            subject_id="organization",
                            relation="viewer",
                            inherited=inherited,
                        )
                    )
                elif scope == "anonymous" and not self._block_external:
                    entries.append(
                        PermissionEntry(
                            subject_type="domain",
                            subject_id="*",
                            relation="viewer",
                        )
                    )

        return entries

    async def get_folder_permissions(self, folder_id: str) -> list[PermissionEntry]:
        """SharePoint supports unique permissions at any level."""
        return await self.get_permissions(folder_id)

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/me")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _map_roles(roles: list[str]) -> str:
    """Map Graph API roles to our relation."""
    for role in roles:
        key = role.lower()
        if key in ROLE_MAP:
            return ROLE_MAP[key]
        if key in ("write", "owner"):
            return "editor"
    return "viewer"


def _parse_timestamp(ts: str) -> datetime:
    if not ts:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
