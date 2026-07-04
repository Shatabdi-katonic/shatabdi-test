"""Dropbox connector.

API: Dropbox HTTP API v2
Auth: OAuth 2.0 access token
Sync: Incremental via server_modified filter on list_folder results
Permissions: sharing/list_file_members per file -- user, group, invitee
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

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

API_BASE = "https://api.dropboxapi.com"
CONTENT_BASE = "https://content.dropboxapi.com"

# Dropbox role tag -> our relation
ROLE_MAP = {
    "owner": "owner",
    "editor": "editor",
    "viewer": "viewer",
    "viewer_no_comment": "viewer",
}

# Content type inference by extension
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


class DropboxConnector(ConnectorBase):
    """Native Dropbox connector using HTTP API v2.

    Config:
        root_path: Folder path to scope the sync. Default "" (entire Dropbox).
        recursive: Whether to list subfolders recursively. Default True.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept the AddKnowledgeModal folder field (dropboxPath) as the root
        # path and dropboxToken as a token fallback (the OAuth access_token is
        # the primary). Without the dropboxPath mapping the folder filter was
        # dropped. Same field-mapping fallback as Outline/ClickUp/Linear.
        self._root_path: str = self._normalise_path(
            config.get("root_path") or config.get("dropboxPath") or ""
        )
        self._recursive: bool = config.get("recursive", True)
        self._config_token: str = config.get("dropboxToken") or ""
        self._client: RetryClient | None = None

    @staticmethod
    def _normalise_path(path: str) -> str:
        """Dropbox expects "" for the root, or a "/"-prefixed path otherwise."""
        path = (path or "").strip()
        if not path or path == "/":
            return ""
        if not path.startswith("/"):
            path = "/" + path
        return path.rstrip("/")

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with an OAuth access token (or the dropboxToken field).

        Expected credentials: {access_token: str}
        """
        token = credentials.get("access_token") or self._config_token
        if not token:
            raise ConnectorAuthError("Missing access_token", connector_type="dropbox")

        self._client = RetryClient(
            base_url=API_BASE,
            headers=bearer_headers(token),
            timeout=60.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify token by calling get_current_account
        try:
            resp = await self._client.post("/2/users/get_current_account")
            data = resp.json()
            logger.info(
                "Dropbox authenticated as %s (%s)",
                data.get("name", {}).get("display_name", "unknown"),
                data.get("email", "unknown"),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                # Surface the real Dropbox error body. A token issued without the
                # account_info.read scope returns 401 "missing_scope" here (NOT a
                # genuinely invalid token) — see OAUTH_PROVIDERS["dropbox"].scopes,
                # which now requests account_info.read (CR-567). Logging the body
                # makes "missing_scope" vs "invalid_access_token" diagnosable.
                try:
                    body = exc.response.text[:300]
                except Exception:
                    body = ""
                logger.warning("Dropbox token verification 401: %s", body)
                raise ConnectorAuthError(
                    f"Invalid or expired Dropbox access token (401: {body})",
                    connector_type="dropbox",
                ) from exc
            raise

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List files in Dropbox, optionally filtered by modification time.

        Uses /2/files/list_folder with cursor-based pagination via
        /2/files/list_folder/continue.
        """
        assert self._client is not None

        payload: dict = {
            "path": self._root_path or "",
            "recursive": self._recursive,
            "include_deleted": False,
            "include_mounted_folders": True,
            "limit": 2000,
        }

        try:
            resp = await self._client.post("/2/files/list_folder", json=payload)
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc)
            raise

        data = resp.json()

        while True:
            for entry in data.get("entries", []):
                # Only yield files, skip folders and deleted entries
                if entry.get(".tag") != "file":
                    continue

                modified_str = entry.get("server_modified", "")
                if modified_str:
                    modified = datetime.fromisoformat(
                        modified_str.replace("Z", "+00:00")
                    )
                else:
                    modified = datetime.now(UTC)

                # Apply incremental filter
                if since and modified < since:
                    continue

                name = entry.get("name", "")
                file_id = entry.get("id", entry.get("path_lower", ""))

                yield DocumentMetadata(
                    external_id=file_id,
                    title=name,
                    url=f"dropbox://{entry.get('path_display', '')}",
                    content_type=_ext_mime(name),
                    size_bytes=entry.get("size"),
                    modified_at=modified,
                    folder_id=entry.get("path_display", "").rsplit("/", 1)[0] or "/",
                    metadata={
                        "path_display": entry.get("path_display", ""),
                        "rev": entry.get("rev", ""),
                        "content_hash": entry.get("content_hash", ""),
                    },
                )

            # Continue pagination if has_more is set
            if not data.get("has_more"):
                break

            cursor = data.get("cursor")
            if not cursor:
                logger.warning("Dropbox pagination: has_more=true but no cursor returned — results may be incomplete")
                break

            try:
                resp = await self._client.post(
                    "/2/files/list_folder/continue",
                    json={"cursor": cursor},
                )
            except httpx.HTTPStatusError as exc:
                _raise_for_status(exc)
                raise
            data = resp.json()

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Download file content from Dropbox.

        Uses the content endpoint /2/files/download with the Dropbox-API-Arg header.
        doc_id can be a Dropbox file ID (id:xxx) or a path.
        """
        assert self._client is not None

        import json as _json

        # The download endpoint uses content.dropboxapi.com
        download_client = RetryClient(
            base_url=CONTENT_BASE,
            headers={
                **self._client._client.headers,
                "Dropbox-API-Arg": _json.dumps({"path": doc_id}),
            },
            timeout=120.0,
        )

        try:
            resp = await download_client.post("/2/files/download")
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc)
            raise
        finally:
            await download_client.close()

        # Parse the result header for metadata
        api_result_str = resp.headers.get("dropbox-api-result", "{}")
        api_result = _json.loads(api_result_str)
        name = api_result.get("name", doc_id)

        return RawDocument(
            external_id=doc_id,
            content=resp.content,
            content_type=_ext_mime(name),
            metadata={"name": name, "rev": api_result.get("rev", "")},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get sharing permissions for a Dropbox file.

        Uses /2/sharing/list_file_members with pagination.
        """
        assert self._client is not None

        permissions: list[PermissionEntry] = []
        payload: dict = {"file": doc_id, "limit": 300}

        try:
            resp = await self._client.post(
                "/2/sharing/list_file_members", json=payload
            )
        except httpx.HTTPStatusError as exc:
            # 409 means the file is not shared or not accessible for sharing
            if exc.response.status_code == 409:
                logger.debug("File %s has no sharing members", doc_id)
                return []
            _raise_for_status(exc)
            raise

        data = resp.json()

        # Process users
        for user in data.get("users", []):
            acct = user.get("user", {})
            role = user.get("access_type", {}).get(".tag", "viewer")
            permissions.append(
                PermissionEntry(
                    subject_type="user",
                    subject_id=acct.get("email", acct.get("account_id", "")),
                    relation=ROLE_MAP.get(role, "viewer"),
                    inherited=user.get("is_inherited", False),
                )
            )

        # Process groups
        for group in data.get("groups", []):
            grp = group.get("group", {})
            role = group.get("access_type", {}).get(".tag", "viewer")
            permissions.append(
                PermissionEntry(
                    subject_type="group",
                    subject_id=grp.get("group_id", ""),
                    relation=ROLE_MAP.get(role, "viewer"),
                    inherited=group.get("is_inherited", False),
                )
            )

        # Process invitees
        for invitee in data.get("invitees", []):
            inv = invitee.get("invitee", {})
            role = invitee.get("access_type", {}).get(".tag", "viewer")
            permissions.append(
                PermissionEntry(
                    subject_type="user",
                    subject_id=inv.get("email", ""),
                    relation=ROLE_MAP.get(role, "viewer"),
                    inherited=False,
                )
            )

        return permissions

    async def health_check(self) -> bool:
        """Verify Dropbox connectivity."""
        if self._client is None:
            return False
        try:
            await self._client.post("/2/users/get_current_account")
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
    if status == 401:
        raise ConnectorAuthError(
            "Dropbox authentication failed", connector_type="dropbox"
        ) from exc
    if status == 429:
        retry_after = float(exc.response.headers.get("Retry-After", "5"))
        raise ConnectorRateLimitError(
            "Dropbox rate limit exceeded",
            connector_type="dropbox",
            retry_after=retry_after,
        ) from exc
    if status >= 500:
        raise ConnectorTransientError(
            f"Dropbox server error {status}", connector_type="dropbox"
        ) from exc
