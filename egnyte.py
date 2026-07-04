"""Egnyte connector.

API: Egnyte Public API v1
Auth: OAuth 2.0 access token
Sync: Recursive folder listing with modification time filter
Permissions: Per-folder permissions via /pubapi/v1/perms endpoint

Egnyte organizes content in a folder hierarchy. This connector recursively
walks folders to discover files and maps Egnyte permission roles to
PermissionEntry objects.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

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

# Egnyte permission levels -> our relation
ROLE_MAP = {
    "Owner": "owner",
    "Full": "editor",
    "Editor": "editor",
    "Viewer": "viewer",
}

EXTENSION_MIMES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".csv": "text/csv",
    ".json": "application/json",
}


def _ext_mime(name: str) -> str:
    """Infer MIME type from filename extension."""
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
        return EXTENSION_MIMES.get(ext, "application/octet-stream")
    return "application/octet-stream"


class EgnyteConnector(ConnectorBase):
    """Native Egnyte connector using the Egnyte Public API.

    Config:
        domain: Egnyte domain (e.g. "mycompany.egnyte.com"). Required.
        root_path: Folder path to scope the sync. Default "Shared".
        max_depth: Maximum recursion depth for folder traversal. Default 20.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._domain: str = config.get("domain", "")
        self._root_path: str = config.get("root_path", "Shared").strip("/")
        self._max_depth: int = config.get("max_depth", 20)
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with an OAuth access token.

        Expected credentials: {access_token: str}
        """
        token = credentials.get("access_token")
        if not token:
            raise ConnectorAuthError(
                "Missing access_token", connector_type="egnyte"
            )
        if not self._domain:
            raise ConnectorAuthError(
                "Egnyte domain is required in config", connector_type="egnyte"
            )

        base_url = f"https://{self._domain}"
        self._client = RetryClient(
            base_url=base_url,
            headers=bearer_headers(token),
            timeout=60.0,
        )

        # Verify token by fetching user info
        try:
            resp = await self._client.get("/pubapi/v1/userinfo")
            data = resp.json()
            logger.info(
                "Egnyte authenticated as %s on %s",
                data.get("username", "unknown"),
                self._domain,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise ConnectorAuthError(
                    "Invalid or expired Egnyte access token",
                    connector_type="egnyte",
                ) from exc
            _raise_for_status(exc)
            raise

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Recursively list files in Egnyte starting from root_path.

        Walks the folder hierarchy using /pubapi/v1/fs/{path} and yields
        DocumentMetadata for each file. Filters by last_modified if since
        is provided.
        """
        assert self._client is not None

        # Stack-based traversal to avoid deep recursion
        folders_to_visit: list[tuple[str, int]] = [(self._root_path, 0)]

        while folders_to_visit:
            folder_path, depth = folders_to_visit.pop()

            if depth > self._max_depth:
                logger.warning(
                    "Egnyte: max depth %d reached at %s, skipping",
                    self._max_depth,
                    folder_path,
                )
                continue

            encoded_path = quote(folder_path, safe="/")
            offset = 0
            count = 100

            while True:
                params = {
                    "list_content": "true",
                    "offset": str(offset),
                    "count": str(count),
                }

                try:
                    resp = await self._client.get(
                        f"/pubapi/v1/fs/{encoded_path}", params=params
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        logger.warning("Egnyte folder not found: %s", folder_path)
                        break
                    _raise_for_status(exc)
                    raise

                data = resp.json()

                # Process files
                for item in data.get("files", []):
                    entry_id = item.get("entry_id", item.get("group_id", ""))
                    name = item.get("name", "")
                    path = item.get("path", f"/{folder_path}/{name}")

                    # Parse modification time (epoch milliseconds)
                    last_modified_ms = item.get("last_modified")
                    if last_modified_ms:
                        modified = datetime.fromtimestamp(
                            last_modified_ms / 1000.0, tz=UTC
                        )
                    else:
                        modified = datetime.now(UTC)

                    if since and modified < since:
                        continue

                    yield DocumentMetadata(
                        external_id=entry_id or path,
                        title=name,
                        url=f"https://{self._domain}/navigate/file/{entry_id}"
                        if entry_id
                        else None,
                        content_type=_ext_mime(name),
                        size_bytes=item.get("size"),
                        modified_at=modified,
                        folder_id=folder_path,
                        metadata={
                            "path": path,
                            "entry_id": entry_id,
                            "lock_info": item.get("lock_info"),
                        },
                    )

                # Queue subfolders
                for subfolder in data.get("folders", []):
                    sub_path = subfolder.get("path", "").lstrip("/")
                    if sub_path:
                        folders_to_visit.append((sub_path, depth + 1))

                # Pagination: count only files for offset (total_count is file count,
                # not file+folder count). Previously mixed both, causing skipped files.
                files = data.get("files", [])
                folders = data.get("folders", [])
                offset += len(files) + len(folders)
                if not files and not folders:
                    break
                total_count = data.get("total_count", 0)
                if total_count and offset >= total_count:
                    break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Download file content from Egnyte.

        doc_id should be the file path (e.g. "Shared/folder/file.pdf")
        or an entry_id. Uses the fs-content endpoint.
        """
        assert self._client is not None

        encoded = quote(doc_id, safe="/")

        try:
            resp = await self._client.get(
                f"/pubapi/v1/fs-content/{encoded}",
            )
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc)
            raise

        content_type = resp.headers.get("content-type", _ext_mime(doc_id))
        filename = doc_id.rsplit("/", 1)[-1] if "/" in doc_id else doc_id

        return RawDocument(
            external_id=doc_id,
            content=resp.content,
            content_type=content_type,
            metadata={"filename": filename},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get permissions for an Egnyte file or folder.

        Uses /pubapi/v1/perms/{path} which returns user and group permissions.
        doc_id should be the folder or file path.
        """
        assert self._client is not None

        encoded = quote(doc_id, safe="/")

        try:
            resp = await self._client.get(f"/pubapi/v1/perms/{encoded}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            if exc.response.status_code == 403:
                logger.debug("No permission to read ACLs for %s", doc_id)
                return []
            _raise_for_status(exc)
            raise

        data = resp.json()
        permissions: list[PermissionEntry] = []

        # User permissions
        for user_email, level in data.get("users", {}).items():
            permissions.append(
                PermissionEntry(
                    subject_type="user",
                    subject_id=user_email,
                    relation=ROLE_MAP.get(level, "viewer"),
                    inherited=False,
                )
            )

        # Group permissions
        for group_name, level in data.get("groups", {}).items():
            permissions.append(
                PermissionEntry(
                    subject_type="group",
                    subject_id=group_name,
                    relation=ROLE_MAP.get(level, "viewer"),
                    inherited=False,
                )
            )

        return permissions

    async def health_check(self) -> bool:
        """Verify Egnyte connectivity."""
        if self._client is None:
            return False
        try:
            await self._client.get("/pubapi/v1/userinfo")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """Release HTTP client resources."""
        if self._client:
            await self._client.close()


def _raise_for_status(exc: httpx.HTTPStatusError) -> None:
    """Convert HTTP errors to connector-specific exceptions."""
    status = exc.response.status_code
    if status in (401, 403):
        raise ConnectorAuthError(
            f"Egnyte authentication/authorization failed ({status})",
            connector_type="egnyte",
        ) from exc
    if status == 429:
        retry_after = float(exc.response.headers.get("Retry-After", "5"))
        raise ConnectorRateLimitError(
            "Egnyte rate limit exceeded",
            connector_type="egnyte",
            retry_after=retry_after,
        ) from exc
    if status >= 500:
        raise ConnectorTransientError(
            f"Egnyte server error {status}", connector_type="egnyte"
        ) from exc
