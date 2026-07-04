"""BookStack connector.

API: BookStack REST API
Auth: Token-based (token_id + token_secret)
Sync: Incremental (updated_at filter) + full
Permissions: Owner-based + role-based content permissions
  - Page and book owners are resolved via the users API
  - Content-level role permissions are fetched when available (BookStack 23.x+)
  - Falls back gracefully to owner-only info on older versions

Content types indexed:
  - Pages (HTML content)
"""

from __future__ import annotations

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

PAGE_LIMIT = 100


class BookStackConnector(ConnectorBase):
    """BookStack wiki connector for pages."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept canonical key (`base_url`) and frontend form key
        # (`bookstackUrl`). AddKnowledgeModal sends `bookstackUrl`
        # directly into config; without this fallback the connector
        # falls back to credentials.base_url (which is empty for
        # API-key sources), and the sync silently lists 0 documents.
        self._base_url: str = (
            config.get("base_url") or config.get("bookstackUrl") or ""
        ).rstrip("/")
        # Hold onto possible config-side token credentials so
        # authenticate() can fall back when the sync route hasn't
        # materialised them into credentials. API-key BookStack
        # sources created via AddKnowledgeModal carry these in config.
        self._fallback_token_id: str = config.get("bookstackTokenId") or ""
        self._fallback_token_secret: str = config.get("bookstackTokenSecret") or ""
        self._client: RetryClient | None = None
        self._users_cache: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using token_id and token_secret.

        Expects credentials: {token_id: str, token_secret: str}
        """
        # Prefer canonical credentials path. Fall back to tokens
        # captured from config in __init__ — that's how API-key
        # BookStack sources created via AddKnowledgeModal carry the
        # token pair today.
        token_id = credentials.get("token_id") or self._fallback_token_id or ""
        token_secret = (
            credentials.get("token_secret") or self._fallback_token_secret or ""
        )
        if not token_id or not token_secret:
            raise ConnectorAuthError(
                "BookStack requires token_id and token_secret",
                connector_type="bookstack",
            )

        if not self._base_url:
            base_url = credentials.get("base_url", "")
            if not base_url:
                raise ConnectorAuthError(
                    "BookStack requires base_url in config or credentials",
                    connector_type="bookstack",
                )
            self._base_url = base_url.rstrip("/")

        self._client = RetryClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Token {token_id}:{token_secret}",
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity
        try:
            await self._client.get_json("/api/pages?count=1&offset=0")
            logger.info("BookStack authenticated at %s", self._base_url)
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"BookStack authentication failed: {e}",
                connector_type="bookstack",
            ) from e

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all pages, optionally filtering by updated_at > since."""
        assert self._client is not None
        offset = 0

        while True:
            try:
                data = await self._client.get_json(
                    "/api/pages",
                    params={"offset": offset, "count": PAGE_LIMIT},
                )
            except Exception as e:
                _raise_typed(e, "bookstack")

            pages = data.get("data", [])
            if not pages:
                break

            for page in pages:
                updated_at = _parse_dt(page.get("updated_at", ""))
                if since and updated_at <= since:
                    continue

                yield DocumentMetadata(
                    external_id=str(page["id"]),
                    title=page.get("name", "Untitled"),
                    url=f"{self._base_url}/books/{page.get('book_id', '')}/page/{page.get('slug', '')}",
                    content_type="text/html",
                    author=None,
                    modified_at=updated_at,
                    metadata={
                        "book_id": page.get("book_id"),
                        "chapter_id": page.get("chapter_id"),
                    },
                )

            total = data.get("total", 0)
            offset += PAGE_LIMIT
            if offset >= total:
                break

    # ------------------------------------------------------------------
    # Fetch document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single page by ID. Returns HTML content."""
        assert self._client is not None

        try:
            data = await self._client.get_json(f"/api/pages/{doc_id}")
        except Exception as e:
            _raise_typed(e, "bookstack")

        html = data.get("html", "")
        name = data.get("name", "Untitled")

        return RawDocument(
            external_id=doc_id,
            content=html.encode("utf-8"),
            content_type="text/html",
            metadata={"title": name},
        )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def _fetch_user_email(self, user_id: int | str) -> str | None:
        """Fetch a user's email by ID, with caching to avoid repeated lookups."""
        assert self._client is not None
        user_id = str(user_id)
        if user_id in self._users_cache:
            return self._users_cache[user_id]
        try:
            user = await self._client.get_json(f"/api/users/{user_id}")
            email = user.get("email", "") or None
            self._users_cache[user_id] = email
            return email
        except Exception:
            self._users_cache[user_id] = None
            return None

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Return permission entries for a BookStack page.

        Resolves the page owner, inherited book owner, and (on BookStack
        23.x+) content-level role permissions.
        """
        assert self._client is not None
        entries: list[PermissionEntry] = []

        # Fetch the page to get ownership and hierarchy info
        try:
            page = await self._client.get_json(f"/api/pages/{doc_id}")
        except Exception:
            return entries

        # --- Page owner ---
        owned_by = page.get("owned_by", {})
        if isinstance(owned_by, dict):
            owner_id = owned_by.get("id")
        else:
            owner_id = owned_by  # older versions may return a bare ID

        if owner_id:
            email = await self._fetch_user_email(owner_id)
            if email:
                entries.append(
                    PermissionEntry(
                        subject_type="user",
                        subject_id=email,
                        relation="owner",
                    )
                )

        # --- Book owner (inherited) ---
        book_id = page.get("book_id")
        if book_id:
            try:
                book = await self._client.get_json(f"/api/books/{book_id}")
                book_owned_by = book.get("owned_by", {})
                book_owner_id = (
                    book_owned_by.get("id")
                    if isinstance(book_owned_by, dict)
                    else book_owned_by
                )
                if book_owner_id and book_owner_id != owner_id:
                    email = await self._fetch_user_email(book_owner_id)
                    if email:
                        entries.append(
                            PermissionEntry(
                                subject_type="user",
                                subject_id=email,
                                relation="editor",
                                inherited=True,
                            )
                        )
            except Exception:
                pass

        # --- Content-level role permissions (BookStack 23.x+) ---
        try:
            perms = await self._client.get_json(
                f"/api/content-permissions/page/{doc_id}"
            )
            for rp in perms.get("role_permissions", []):
                role_id = rp.get("role_id")
                if not role_id:
                    continue
                relation = "editor" if rp.get("update") else "viewer"
                entries.append(
                    PermissionEntry(
                        subject_type="group",
                        subject_id=f"bookstack_role:{role_id}",
                        relation=relation,
                    )
                )
        except Exception:
            # Older BookStack without content-permissions endpoint
            pass

        return entries

    async def get_folder_permissions(self, folder_id: str) -> list[PermissionEntry]:
        """Return permission entries for a BookStack book (folder).

        Resolves the book owner and content-level role permissions when
        the newer BookStack API is available.
        """
        assert self._client is not None
        entries: list[PermissionEntry] = []

        # Fetch the book
        try:
            book = await self._client.get_json(f"/api/books/{folder_id}")
        except Exception:
            return entries

        # --- Book owner ---
        owned_by = book.get("owned_by", {})
        owner_id = owned_by.get("id") if isinstance(owned_by, dict) else owned_by

        if owner_id:
            email = await self._fetch_user_email(owner_id)
            if email:
                entries.append(
                    PermissionEntry(
                        subject_type="user",
                        subject_id=email,
                        relation="owner",
                    )
                )

        # --- Content-level role permissions (BookStack 23.x+) ---
        try:
            perms = await self._client.get_json(
                f"/api/content-permissions/book/{folder_id}"
            )
            for rp in perms.get("role_permissions", []):
                role_id = rp.get("role_id")
                if not role_id:
                    continue
                relation = "editor" if rp.get("update") else "viewer"
                entries.append(
                    PermissionEntry(
                        subject_type="group",
                        subject_id=f"bookstack_role:{role_id}",
                        relation=relation,
                    )
                )
        except Exception:
            pass

        return entries

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/api/pages?count=1&offset=0")
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
    """Parse an ISO-ish datetime string from BookStack."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _raise_typed(exc: Exception, connector_type: str) -> None:
    """Re-raise a generic exception as the appropriate ConnectorError subclass."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401 or code == 403:
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