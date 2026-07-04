"""Confluence connector.

API: Confluence REST API v2 (Atlassian Cloud) or v1 (Server/DC)
Auth: OAuth 2.0 (Cloud) or API token + email (Server/DC)
Sync: Incremental (lastModified) + full
Permissions: Space permissions + page restrictions

Role mapping (spec section 15.3):
  Space admin       -> editor (folder-level)
  Space viewer      -> viewer (folder-level)
  Page restriction  -> overrides space permissions (document-level)
  Anonymous access  -> BLOCKED
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx

from platform_knowledge_engine.connectors._utils.http_client import (
    RetryClient,
    bearer_headers,
)
from platform_knowledge_engine.connectors.base import (
    ConnectorAuthError,
    ConnectorBase,
    ConnectorNotFoundError,
    ConnectorTransientError,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)


def _raise_mapped(exc: Exception) -> None:
    """Translate a raw httpx error into a typed connector error.

    ``RetryClient.get_json`` surfaces ``httpx.HTTPStatusError`` for un-retried
    4xx responses (429 and 5xx are retried internally). Confluence's
    ``fetch_document`` / ``get_permissions`` previously let that raw error
    propagate, so the sync path saw an opaque "Client error 401 Unauthorized"
    and could not tell an access revocation (401/403) apart from a page that
    was deleted (404) or a transient blip. Classifying it lets the sync loop
    react correctly — purge a revoked/removed page's chunks, but leave a
    transiently-failing one alone. Mirrors ``coda._raise_mapped``. No-op for
    non-httpx errors so the caller can re-raise the original.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            raise ConnectorAuthError(str(exc), connector_type="confluence") from exc
        if code == 404:
            raise ConnectorNotFoundError(str(exc), connector_type="confluence") from exc
        raise ConnectorTransientError(str(exc), connector_type="confluence") from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type="confluence") from exc


def _has_text(markup: str) -> bool:
    """True if storage/HTML markup contains any human-readable text.

    Live-doc / new-editor pages can return a NON-empty storage stub (empty
    ADF wrapper, layout macros) with no actual text — a bare ``.strip()`` check
    treats that as "has content" and skips the ADF fallback. Strip tags/entities
    and check for real characters instead.
    """
    if not markup:
        return False
    text = re.sub(r"<[^>]+>", " ", markup)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return bool(text.strip())


def _adf_to_text(node: dict) -> str:
    """Extract plain text from an Atlassian Document Format (ADF) node tree.

    New-editor / "live doc" Confluence pages expose their body only as ADF
    (body-format=storage is empty), so we walk the ADF and pull the text.
    """
    parts: list[str] = []

    def _walk(n: dict) -> None:
        if not isinstance(n, dict):
            return
        if n.get("type") == "text":
            parts.append(n.get("text", ""))
        elif n.get("type") == "hardBreak":
            parts.append("\n")
        for child in n.get("content", []) or []:
            _walk(child)
        if n.get("type") in ("paragraph", "heading", "blockquote", "listItem", "tableCell"):
            parts.append("\n")

    _walk(node)
    return "".join(parts).strip()


def _storage_to_html(storage: str) -> str:
    """Convert Confluence storage-format XHTML into plain HTML the parser reads.

    Confluence storage format wraps page content in custom-namespace tags —
    ``<ac:layout>`` / ``<ac:layout-cell>`` (page layout), ``<ac:structured-macro>``
    (macros), ``<ri:*>`` (resource refs) — with NO ``<html>/<body>`` root. The
    ``unstructured`` HTML partitioner extracts NOTHING from that: a page wrapped
    in ``<ac:layout>`` yields 0 elements even though it holds real ``<p>``/``<h2>``
    prose, so macro/layout-heavy pages ingested as "completed, 0 chunks (empty
    or unparseable)". Strip the Confluence-only tags but KEEP their standard-HTML
    children so the real text survives chunking. Best-effort — any parse error
    returns the original string unchanged.
    """
    if not storage or ("<ac:" not in storage and "<ri:" not in storage):
        # Empty, or already plain HTML (older / simple pages) — nothing to do.
        return storage or ""
    # Whether the page embeds images/attachments. Kept as an HTML comment on the
    # cleaned output (comments are NOT extracted as text, so an image-only page
    # still parses to 0 elements) so the ingest path can label such a page
    # "image — not supported" instead of the generic "empty or unparseable".
    has_media = "<ac:image" in storage or "<ri:attachment" in storage
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(storage, "html.parser")
        # Drop pure-noise nodes first: macro parameters and resource references
        # carry no readable prose (ids, filenames, config) and pollute output.
        for tag in soup.find_all(True):
            name = tag.name or ""
            if name.startswith("ac:parameter") or name.startswith("ri:"):
                tag.decompose()
        # Unwrap the remaining Confluence structural / macro tags, keeping the
        # standard HTML (<p>, <h1-6>, <ul>/<li>, <table>, <a>, <span>, …) inside
        # so unstructured can extract it.
        for tag in soup.find_all(True):
            name = tag.name or ""
            if name.startswith("ac:") or name.startswith("ri:"):
                tag.unwrap()
        marker = "<!--ke-media-image-->" if has_media else ""
        return f"<html><body>{soup}{marker}</body></html>"
    except Exception as exc:  # noqa: BLE001 - cleaning is best-effort
        logger.warning("confluence_storage_clean_failed: %s", exc)
        return storage


class ConfluenceConnector(ConnectorBase):
    """Native Confluence connector.

    Config:
        base_url: Confluence instance URL (e.g., https://acme.atlassian.net/wiki)
        space_keys: List of space keys to sync. Empty = all accessible spaces.
        is_cloud: True for Atlassian Cloud, False for Server/DC.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Support both frontend field names and canonical names
        self._base_url: str = (config.get("base_url") or config.get("confluenceUrl") or "").rstrip("/")
        # Human-facing site URL used to build page links (e.g. citations/
        # downloads). For Server/DC and explicit base_url configs this is
        # the same as _base_url. For Cloud OAuth, _base_url gets overwritten
        # in authenticate() with the api.atlassian.com gateway URL (needed
        # for REST calls), which is NOT browsable — it requires the
        # Atlassian web session and otherwise 404s via a login redirect.
        # _site_url is set separately during OAuth discovery to the actual
        # browsable instance URL (e.g. https://acme.atlassian.net).
        self._site_url: str = self._base_url
        raw_keys = config.get("space_keys") or config.get("confluenceSpaceKey") or ""
        self._space_keys: list[str] = (
            raw_keys if isinstance(raw_keys, list)
            else [k.strip() for k in raw_keys.split(",") if k.strip()]
        )
        # Auto-detect Cloud vs Server from URL if not explicitly set
        if "is_cloud" in config:
            self._is_cloud: bool = config["is_cloud"]
        else:
            self._is_cloud = ".atlassian.net" in self._base_url
        self._client: RetryClient | None = None
        # Captured in authenticate() so fetch_document can build a v1 REST
        # client for the rendered-view body fallback (live docs).
        self._headers: dict[str, str] = {}

    async def authenticate(self, credentials: dict) -> None:
        headers: dict[str, str] = {}

        if "access_token" in credentials:
            # OAuth (Cloud)
            headers = bearer_headers(credentials["access_token"])

            # Auto-discover Confluence site URL from Atlassian accessible-resources API
            # when base_url is not provided (typical for OAuth flows where the frontend
            # doesn't know the site URL).
            if not self._base_url:
                try:
                    discovery_client = RetryClient(
                        base_url="https://api.atlassian.com",
                        headers=headers,
                    )
                    resources = await discovery_client.get_json(
                        "/oauth/token/accessible-resources"
                    )
                    await discovery_client.close()

                    # Find the first Confluence-capable site
                    for resource in resources:
                        scopes = resource.get("scopes", [])
                        # Check if this resource has Confluence access
                        if any("confluence" in s for s in scopes) or not scopes:
                            cloud_id = resource.get("id")
                            if cloud_id:
                                self._base_url = f"https://api.atlassian.com/ex/confluence/{cloud_id}"
                                self._is_cloud = True
                                # resource["url"] is the actual browsable
                                # Confluence site (e.g. https://acme.atlassian.net),
                                # distinct from the api.atlassian.com gateway URL
                                # above, which is REST-only.
                                self._site_url = (
                                    resource.get("url", "").rstrip("/") or self._base_url
                                )
                                logger.info(
                                    "Auto-discovered Confluence site: %s (cloud_id=%s, name=%s)",
                                    self._base_url, cloud_id, resource.get("name", "unknown"),
                                )
                                break

                    if not self._base_url:
                        raise ConnectorAuthError(
                            "No Confluence site found in accessible resources. "
                            "Ensure the OAuth app has Confluence API access.",
                            connector_type="confluence",
                        )
                except ConnectorAuthError:
                    raise
                except Exception as e:
                    raise ConnectorAuthError(
                        f"Failed to discover Confluence site: {e}",
                        connector_type="confluence",
                    ) from e

        elif credentials.get("api_token") or credentials.get("confluenceToken"):
            # API token (Server/DC or Cloud PAT)
            import base64

            token = credentials.get("api_token") or credentials.get("confluenceToken", "")
            email = credentials.get("email") or credentials.get("confluenceEmail", "")
            if not email:
                raise ConnectorAuthError(
                    "Email is required for API token authentication",
                    connector_type="confluence",
                )
            cred_str = f"{email}:{token}"
            b64 = base64.b64encode(cred_str.encode()).decode()
            headers = {"Authorization": f"Basic {b64}"}
        else:
            raise ConnectorAuthError(
                "Confluence requires either access_token (OAuth) or api_token + email",
                connector_type="confluence",
            )

        if not self._base_url:
            raise ConnectorAuthError("Confluence URL is required", connector_type="confluence")

        api_base = f"{self._base_url}/api/v2" if self._is_cloud else f"{self._base_url}/rest/api"
        self._headers = headers
        self._client = RetryClient(base_url=api_base, headers=headers)

        # Verify credentials
        try:
            if self._is_cloud:
                await self._client.get_json("/spaces", params={"limit": "1"})
            else:
                await self._client.get_json("/space", params={"limit": "1"})
        except Exception as e:
            raise ConnectorAuthError(
                f"Confluence authentication failed: {e}",
                connector_type="confluence",
            ) from e

        logger.info("Confluence authenticated at %s (cloud=%s)", self._base_url, self._is_cloud)

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None

        spaces = self._space_keys or await self._get_all_space_keys()

        for space_key in spaces:
            try:
                async for doc in self._list_space_pages(space_key, since):
                    yield doc
            except Exception as e:
                logger.error("confluence_list_space_failed", space_key=space_key, error=str(e))
                raise ConnectorTransientError(
                    f"Failed to list pages in space {space_key}: {e}",
                    connector_type="confluence",
                ) from e

    async def _get_all_space_keys(self) -> list[str]:
        """Get all accessible space keys."""
        assert self._client is not None
        keys: list[str] = []

        if self._is_cloud:
            url = "/spaces"
            cursor: str | None = None
            while True:
                params: dict = {"limit": "100"}
                if cursor:
                    params["cursor"] = cursor
                data = await self._client.get_json(url, params=params)
                for space in data.get("results", []):
                    keys.append(space["key"])
                cursor = _extract_cursor(data.get("_links", {}).get("next"))
                if not cursor:
                    break
        else:
            data = await self._client.get_json("/space", params={"limit": "500"})
            for space in data.get("results", []):
                keys.append(space["key"])

        return keys

    async def _resolve_space_id(self, space_key: str) -> str | None:
        """Resolve a Confluence Cloud space KEY to its numeric v2 space id.

        The v2 /pages endpoint filters by `space-id` (numeric) — it has NO
        `space-key` parameter, so passing one is silently ignored and /pages
        returns pages from EVERY space (the whole instance). We must look up
        the id first. Works for personal spaces (keys starting with '~') too.
        """
        assert self._client is not None
        try:
            data = await self._client.get_json(
                "/spaces", params={"keys": space_key, "limit": "1"}
            )
            results = data.get("results", [])
            if results and results[0].get("id") is not None:
                return str(results[0]["id"])
        except Exception as e:
            logger.warning(
                "confluence_resolve_space_id_failed", space_key=space_key, error=str(e)
            )
        return None

    async def _list_space_pages(
        self,
        space_key: str,
        since: datetime | None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List pages in a space."""
        assert self._client is not None

        if self._is_cloud:
            # v2 API: pages endpoint. MUST filter by numeric space-id — the v2
            # /pages endpoint has no space-key param, so a space-key filter is
            # ignored and every space's pages come back. Resolve key -> id.
            space_id = await self._resolve_space_id(space_key)
            if not space_id:
                logger.warning(
                    "confluence_space_not_found",
                    space_key=space_key,
                    detail="No space matched this key; skipping (nothing synced for it).",
                )
                return
            url = "/pages"
            cursor: str | None = None
            while True:
                params: dict = {"space-id": space_id, "limit": "100", "sort": "-modified-date"}
                if cursor:
                    params["cursor"] = cursor
                data = await self._client.get_json(url, params=params)

                for page in data.get("results", []):
                    modified = _parse_timestamp(page.get("version", {}).get("createdAt", ""))
                    if since and modified < since:
                        continue

                    yield DocumentMetadata(
                        external_id=page["id"],
                        title=page.get("title", "Untitled"),
                        url=f"{self._site_url}/wiki{page.get('_links', {}).get('webui', '')}",
                        content_type="text/html",
                        author=page.get("version", {}).get("authorId"),
                        modified_at=modified,
                        folder_id=space_key,
                        metadata={"space_key": space_key, "status": page.get("status")},
                    )

                cursor = _extract_cursor(data.get("_links", {}).get("next"))
                if not cursor:
                    break
        else:
            # v1 API: content endpoint
            start = 0
            while True:
                params = {
                    "spaceKey": space_key,
                    "type": "page",
                    "expand": "version",
                    "limit": "100",
                    "start": str(start),
                }
                data = await self._client.get_json("/content", params=params)

                for page in data.get("results", []):
                    modified = _parse_timestamp(page.get("version", {}).get("when", ""))
                    if since and modified < since:
                        continue

                    yield DocumentMetadata(
                        external_id=page["id"],
                        title=page.get("title", "Untitled"),
                        url=f"{self._base_url}{page.get('_links', {}).get('webui', '')}",
                        content_type="text/html",
                        author=page.get("version", {}).get("by", {}).get("email"),
                        modified_at=modified,
                        folder_id=space_key,
                        metadata={"space_key": space_key},
                    )

                if data.get("size", 0) < 100:
                    break
                start += 100

    async def _fetch_cloud_v1_view(self, doc_id: str) -> str:
        """Fetch a page's rendered HTML via the v1 REST API (fallback).

        For live-doc / new-editor pages the v2 storage and ADF bodies can both
        be empty; the v1 ``body.view`` is the rendered HTML Confluence actually
        shows and normally contains the text. Best-effort — returns "" on any
        error (e.g. v1 not permitted by the token's granular scopes).
        """
        if not self._is_cloud or not self._base_url:
            return ""
        client = RetryClient(
            base_url=f"{self._base_url}/rest/api", headers=self._headers
        )
        try:
            data = await client.get_json(
                f"/content/{doc_id}", params={"expand": "body.view"}
            )
            return data.get("body", {}).get("view", {}).get("value", "") or ""
        except Exception as e:
            logger.warning(
                "confluence_v1_view_fallback_failed doc=%s err=%s", doc_id, e
            )
            return ""
        finally:
            await client.close()

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch page content as storage format HTML.

        Classifies HTTP failures (via ``_raise_mapped``) so the sync path can
        distinguish an access revocation / deleted page (401/403/404 → not
        retryable, content should be purged) from a transient error.
        """
        assert self._client is not None

        try:
            if self._is_cloud:
                data = await self._client.get_json(
                    f"/pages/{doc_id}",
                    params={"body-format": "storage"},
                )
                body = data.get("body", {}).get("storage", {}).get("value", "")
                # New-editor / "live doc" pages return a storage body with NO
                # readable text (an empty ADF wrapper / layout stub) — not "" —
                # so they'd index as 0 chunks even though the page has text. When
                # storage has no extractable text, fall back to the ADF body.
                if not _has_text(body):
                    try:
                        adf_data = await self._client.get_json(
                            f"/pages/{doc_id}",
                            params={"body-format": "atlas_doc_format"},
                        )
                        adf_raw = (
                            adf_data.get("body", {})
                            .get("atlas_doc_format", {})
                            .get("value", "")
                        )
                        adf_text = ""
                        if adf_raw:
                            import json as _json

                            adf_text = _adf_to_text(_json.loads(adf_raw))
                        logger.info(
                            "confluence_storage_no_text_tried_adf doc=%s storage_len=%d adf_chars=%d",
                            doc_id,
                            len(body or ""),
                            len(adf_text),
                        )
                        if adf_text:
                            body = adf_text
                    except Exception as adf_err:
                        logger.warning(
                            "confluence_adf_fallback_failed doc=%s err=%s",
                            doc_id,
                            adf_err,
                        )

                    # Last resort: the v1 rendered "view" body. Live docs can
                    # return empty storage AND empty ADF on v2; the v1 view is
                    # the HTML Confluence actually displays and usually has text.
                    if not _has_text(body):
                        view_html = await self._fetch_cloud_v1_view(doc_id)
                        if _has_text(view_html):
                            body = view_html
                            logger.info(
                                "confluence_used_v1_view doc=%s chars=%d",
                                doc_id,
                                len(view_html),
                            )
            else:
                data = await self._client.get_json(
                    f"/content/{doc_id}",
                    params={"expand": "body.storage"},
                )
                body = data.get("body", {}).get("storage", {}).get("value", "")
        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            _raise_mapped(e)
            raise  # unreachable — _raise_mapped always raises for these

        # Confluence returns storage-format XHTML (<ac:*>/<ri:*> macro tags)
        # that the text parser can't read — clean it to plain HTML so
        # macro/layout-wrapped pages don't ingest as "0 chunks (unparseable)".
        body = _storage_to_html(body)

        return RawDocument(
            external_id=doc_id,
            content=body.encode("utf-8"),
            content_type="text/html",
            metadata={"title": data.get("title", "")},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get page restrictions. Pages without restrictions inherit space permissions."""
        assert self._client is not None
        entries: list[PermissionEntry] = []

        try:
            if self._is_cloud:
                # Check page restrictions via operations
                data = await self._client.get_json(
                    f"/pages/{doc_id}",
                    params={"include-operations": "true"},
                )
                # If no restrictions, space permissions apply (handled by folder perms)
                restrictions = data.get("restrictions", {})
                if not restrictions:
                    return entries

                for restriction_type in ["read", "update"]:
                    r = restrictions.get(restriction_type, {})
                    relation = "viewer" if restriction_type == "read" else "editor"
                    for user in r.get("users", []):
                        entries.append(
                            PermissionEntry(
                                subject_type="user",
                                subject_id=user.get("email", user.get("accountId", "")),
                                relation=relation,
                            )
                        )
                    for group in r.get("groups", []):
                        entries.append(
                            PermissionEntry(
                                subject_type="group",
                                subject_id=group.get("name", group.get("id", "")),
                                relation=relation,
                            )
                        )
            else:
                data = await self._client.get_json(
                    f"/content/{doc_id}/restriction",
                )
                for r in data.get("results", []):
                    operation = r.get("operation", "read")
                    relation = "viewer" if operation == "read" else "editor"
                    for user in r.get("restrictions", {}).get("user", {}).get("results", []):
                        entries.append(
                            PermissionEntry(
                                subject_type="user",
                                subject_id=user.get("username", user.get("email", "")),
                                relation=relation,
                            )
                        )
                    for group in r.get("restrictions", {}).get("group", {}).get("results", []):
                        entries.append(
                            PermissionEntry(
                                subject_type="group",
                                subject_id=group.get("name", ""),
                                relation=relation,
                            )
                        )
        except Exception as e:
            logger.warning("Failed to get restrictions for page %s: %s", doc_id, e)

        return entries

    async def get_folder_permissions(self, folder_id: str) -> list[PermissionEntry]:
        """Get space-level permissions (folder_id = space_key)."""
        assert self._client is not None
        entries: list[PermissionEntry] = []

        try:
            if self._is_cloud:
                data = await self._client.get_json(f"/spaces/{folder_id}/permissions")
                for perm in data.get("results", []):
                    principal = perm.get("principal", {})
                    p_type = principal.get("type", "")
                    p_id = principal.get("id", "")
                    operation = perm.get("operation", {}).get("key", "read")
                    relation = (
                        "editor" if operation in ("administer", "create", "delete") else "viewer"
                    )

                    if p_type == "user":
                        entries.append(PermissionEntry("user", p_id, relation))
                    elif p_type == "group":
                        entries.append(PermissionEntry("group", p_id, relation))
        except Exception as e:
            logger.warning("Failed to get space permissions for %s: %s", folder_id, e)

        return entries

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            if self._is_cloud:
                await self._client.get_json("/spaces", params={"limit": "1"})
            else:
                await self._client.get_json("/space", params={"limit": "1"})
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _extract_cursor(next_link: str | None) -> str | None:
    """Extract the cursor value from a Confluence Cloud v2 _links.next field.

    The v2 API returns _links.next as a full relative URL like:
      /wiki/api/v2/pages?cursor=eyJ...&limit=100
    This function extracts just the cursor parameter value.
    If next_link is already a plain cursor token (no path), return it as-is.
    """
    if not next_link:
        return None
    # If it looks like a URL path (contains / or ?), extract the cursor param
    if "/" in next_link or "?" in next_link:
        parsed = urlparse(next_link)
        qs = parse_qs(parsed.query)
        cursor_values = qs.get("cursor", [])
        return cursor_values[0] if cursor_values else None
    # Already a plain cursor token
    return next_link


def _parse_timestamp(ts: str) -> datetime:
    if not ts:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
