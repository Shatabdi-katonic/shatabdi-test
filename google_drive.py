"""Google Drive connector.

API: Google Drive API v3
Auth: OAuth 2.0 (or service account JSON)
Sync: Incremental (modifiedTime filter) + full
Permissions: permissions.list() per file -- user, group, domain, anyone

Role mapping (spec section 15.1):
  owner                     -> owner
  organizer, fileOrganizer  -> editor
  writer                    -> editor
  commenter, reader         -> viewer
  domain sharing            -> viewer (flagged)
  anyone with link          -> BLOCKED by default
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import (
    RetryClient,
    bearer_headers,
    paginate_google,
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

DRIVE_API = "https://www.googleapis.com/drive/v3"

# Google Workspace MIME types that require export (not binary download)
EXPORT_MIMES: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("application/pdf", "pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pptx",
    ),
    "application/vnd.google-apps.drawing": ("application/pdf", "pdf"),
}

# Skip these Google Workspace types entirely
SKIP_MIMES = {
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.fusiontable",
    "application/vnd.google-apps.jam",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.folder",
}

ROLE_MAP = {
    "owner": "owner",
    "organizer": "editor",
    "fileOrganizer": "editor",
    "writer": "editor",
    "commenter": "viewer",
    "reader": "viewer",
}

# Fields to request from files.list
FILE_FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,owners,parents,trashed,capabilities"


class GoogleDriveConnector(ConnectorBase):
    """Native Google Drive connector using Drive API v3.

    Config:
        folder_ids: Optional list of folder IDs to scope the sync.
                    If empty, syncs the entire drive accessible to the user.
        include_shared_drives: Whether to include shared (team) drives.
        block_anyone_links: Block 'anyone with link' files (default True).
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._config = config
        self._folder_ids: list[str] = config.get("folder_ids", [])

        # Frontend may send a Google Drive folder URL as "gdriveUrl"
        if not self._folder_ids:
            gdrive_url = config.get("gdriveUrl", "")
            if gdrive_url:
                fid = self._parse_folder_id(gdrive_url)
                if fid:
                    self._folder_ids = [fid]

        self._include_shared: bool = config.get("include_shared_drives", True)
        self._block_anyone: bool = config.get("block_anyone_links", True)
        self._client: RetryClient | None = None
        # Owner identifier captured at authenticate-time. Belt-and-suspenders
        # safety net: get_permissions() walks the real Google Drive ACL via
        # /files/{id}/permissions, which returns user/group/domain entries
        # keyed by EMAIL ADDRESS. IdentityResolver then maps each email to
        # a Keycloak sub via the credential-store mapping registered at
        # connector-create time. If the OAuth user's email-to-sub mapping
        # is missing (e.g. their Google email differs from the email
        # Keycloak knows about), every Drive entry gets dropped and the
        # retriever permission filter silently filters out all chunks —
        # same phantom-chunks symptom as the Miro bug, for a different
        # reason. Prepending a static owner entry tied to the canonical
        # platform_user_id (Keycloak sub) guarantees the OAuth user
        # retains access regardless of email mapping. The real Drive ACL
        # is still appended on top so co-owners / viewers continue to work.
        self._owner_user_id: str = ""

    @staticmethod
    def _parse_folder_id(url: str) -> str | None:
        """Extract a Google Drive folder ID from a URL like
        https://drive.google.com/drive/folders/<FOLDER_ID>..."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        # Expect path like /drive/folders/<id>
        if "folders" in parts:
            idx = parts.index("folders")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        return None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        # Inline sync may pass config as credentials; check config too
        if not token:
            token = self._config.get("access_token", "")
        if not token:
            raise ConnectorAuthError(
                "Google Drive connector requires access_token (via OAuth or config)",
                connector_type="google_drive",
            )
        # Prefer the canonical platform user_id (Keycloak sub) injected by
        # the OAuth callback as ``platform_user_id``. Falls back to the
        # provider-native ``user_id`` for pre-fix records. See miro.py
        # for the full bug history; same fallback pattern as the other
        # connectors fixed in commit bbf650fd.
        self._owner_user_id = (
            credentials.get("platform_user_id")
            or credentials.get("user_id")
            or ""
        ).strip()
        self._client = RetryClient(
            base_url=DRIVE_API,
            headers=bearer_headers(token),
        )
        # Verify connectivity
        about = await self._client.get_json("/about", params={"fields": "user"})
        user_email = about.get("user", {}).get("emailAddress", "unknown")
        logger.info(
            "Google Drive authenticated as %s (owner=%s)",
            user_email, self._owner_user_id or "?",
        )

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None

        # Try Changes API for efficient incremental sync if we have a page token
        start_page_token = self._config.get("changes_page_token")
        if since and start_page_token:
            async for doc in self._list_via_changes(start_page_token):
                yield doc
            return

        q_parts = ["trashed = false"]

        # Scope to specific folders if configured
        if self._folder_ids:
            parents = " or ".join(f"'{fid}' in parents" for fid in self._folder_ids)
            q_parts.append(f"({parents})")

        # Exclude folders themselves
        q_parts.append("mimeType != 'application/vnd.google-apps.folder'")

        # Incremental fallback: only files modified after `since`
        if since:
            ts = since.strftime("%Y-%m-%dT%H:%M:%S")
            q_parts.append(f"modifiedTime > '{ts}'")

        query = " and ".join(q_parts)

        params = {
            "q": query,
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "orderBy": "modifiedTime desc",
            "supportsAllDrives": str(self._include_shared).lower(),
            "includeItemsFromAllDrives": str(self._include_shared).lower(),
        }

        try:
            async for file in paginate_google(self._client, "/files", params=params):
                mime = file.get("mimeType", "")
                if mime in SKIP_MIMES:
                    continue

                # Determine content type for export
                if mime in EXPORT_MIMES:
                    content_type = EXPORT_MIMES[mime][0]
                else:
                    content_type = mime

                owners = file.get("owners", [])
                author = owners[0].get("emailAddress", "") if owners else None

                yield DocumentMetadata(
                    external_id=file["id"],
                    title=file.get("name", "Untitled"),
                    url=file.get("webViewLink"),
                    content_type=content_type,
                    size_bytes=int(file["size"]) if file.get("size") else None,
                    author=author,
                    modified_at=_parse_timestamp(file.get("modifiedTime", "")),
                    folder_id=file.get("parents", [None])[0] if file.get("parents") else None,
                    metadata={
                        "mime_type": mime,
                        "drive_id": file.get("driveId"),
                    },
                )
        except ConnectorTransientError:
            raise
        except Exception as e:
            logger.error("Error listing Google Drive files: %s", e)
            raise ConnectorTransientError(
                f"Error listing Google Drive files: {e}",
                connector_type="google_drive",
            ) from e

    async def _list_via_changes(self, start_page_token: str) -> AsyncIterator[DocumentMetadata]:
        """Use the Google Drive Changes API for efficient incremental sync.

        The Changes API returns only files that changed since the last page token,
        including deletions. This is ~10x more efficient than re-scanning with
        modifiedTime filter for large drives.
        """
        params = {
            "pageToken": start_page_token,
            "pageSize": "1000",
            "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))",
            "supportsAllDrives": str(self._include_shared).lower(),
            "includeItemsFromAllDrives": str(self._include_shared).lower(),
        }

        while True:
            data = await self._client.get_json("/changes", params=params)

            for change in data.get("changes", []):
                if change.get("removed"):
                    # File was deleted — yield with a special metadata flag
                    yield DocumentMetadata(
                        external_id=change["fileId"],
                        title="(deleted)",
                        content_type="application/deleted",
                        metadata={"deleted": True},
                    )
                    continue

                file = change.get("file")
                if not file:
                    continue
                mime = file.get("mimeType", "")
                if mime in SKIP_MIMES:
                    continue

                if mime in EXPORT_MIMES:
                    content_type = EXPORT_MIMES[mime][0]
                else:
                    content_type = mime or "application/octet-stream"

                yield DocumentMetadata(
                    external_id=file["id"],
                    title=file.get("name", ""),
                    url=file.get("webViewLink"),
                    content_type=content_type,
                    size_bytes=int(file.get("size", 0)) if file.get("size") else None,
                    author=(file.get("owners") or [{}])[0].get("emailAddress"),
                    modified_at=_parse_timestamp(file.get("modifiedTime", "")),
                    metadata={"mime": mime},
                )

            # Store new page token for next sync
            new_token = data.get("newStartPageToken")
            if new_token:
                self._config["changes_page_token"] = new_token
                # Sentinel metadata entry so the pipeline can persist the token
                yield DocumentMetadata(
                    external_id="__changes_page_token__",
                    title="",
                    content_type="application/x-sync-state",
                    metadata={"changes_page_token": new_token, "_sync_state": True},
                )

            next_page = data.get("nextPageToken")
            if not next_page:
                break
            params["pageToken"] = next_page

    async def get_start_page_token(self) -> str:
        """Get the initial Changes API start page token.

        Call once after the first full sync to enable incremental
        sync via Changes API on subsequent syncs.
        """
        assert self._client is not None
        data = await self._client.get_json("/changes/startPageToken", params={
            "supportsAllDrives": str(self._include_shared).lower(),
        })
        return data.get("startPageToken", "")

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch document content. Exports Google Workspace files to standard formats."""
        assert self._client is not None

        # Get file metadata to check MIME type
        meta = await self._client.get_json(
            f"/files/{doc_id}",
            params={"fields": "mimeType,name", "supportsAllDrives": "true"},
        )
        mime = meta.get("mimeType", "")
        name = meta.get("name", doc_id)

        if mime in EXPORT_MIMES:
            # Export Google Workspace files
            export_mime, ext = EXPORT_MIMES[mime]
            resp = await self._client.get(
                f"/files/{doc_id}/export",
                params={"mimeType": export_mime},
            )
            return RawDocument(
                external_id=doc_id,
                content=resp.content,
                content_type=export_mime,
                metadata={"original_name": name, "original_mime": mime},
            )
        else:
            # Binary download for uploaded files
            resp = await self._client.get(
                f"/files/{doc_id}",
                params={"alt": "media", "supportsAllDrives": "true"},
            )
            return RawDocument(
                external_id=doc_id,
                content=resp.content,
                content_type=mime,
                metadata={"original_name": name},
            )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Extract permissions via permissions.list().

        Handles user, group, domain, and anyone permission types.
        'Anyone with link' is blocked by default (admin override via config).

        Belt-and-suspenders: prepend an owner entry tied to the canonical
        platform_user_id (Keycloak sub) BEFORE walking the real Drive ACL.
        Drive ACL entries are keyed by email; IdentityResolver maps each
        email to a sub via the credential-store mapping. If that mapping
        is missing (Google email != Keycloak email), every Drive entry
        gets dropped silently and the retriever permission filter
        excludes every chunk from search results — same phantom-chunks
        symptom as the Miro bug for a different reason. The static
        owner entry guarantees the OAuth user retains access regardless
        of email-to-sub resolution success.
        """
        assert self._client is not None
        entries: list[PermissionEntry] = []

        # Always start with the canonical owner entry — see docstring.
        # This entry uses the Keycloak sub directly, so it bypasses
        # IdentityResolver email lookup entirely.
        if self._owner_user_id:
            entries.append(
                PermissionEntry(
                    subject_type="user",
                    subject_id=self._owner_user_id,
                    relation="owner",
                )
            )

        try:
            data = await self._client.get_json(
                f"/files/{doc_id}/permissions",
                params={
                    "fields": "permissions(id,type,role,emailAddress,domain)",
                    "supportsAllDrives": "true",
                },
            )
        except Exception as e:
            logger.warning("Failed to get permissions for %s: %s", doc_id, e)
            return entries

        for perm in data.get("permissions", []):
            perm_type = perm.get("type", "")
            role = perm.get("role", "reader")
            mapped_role = ROLE_MAP.get(role, "viewer")

            if perm_type == "user":
                email = perm.get("emailAddress", "")
                if email:
                    entries.append(
                        PermissionEntry(
                            subject_type="user",
                            subject_id=email,
                            relation=mapped_role,
                        )
                    )

            elif perm_type == "group":
                email = perm.get("emailAddress", "")
                if email:
                    entries.append(
                        PermissionEntry(
                            subject_type="group",
                            subject_id=email,
                            relation=mapped_role,
                        )
                    )

            elif perm_type == "domain":
                domain = perm.get("domain", "")
                if domain:
                    entries.append(
                        PermissionEntry(
                            subject_type="domain",
                            subject_id=domain,
                            relation="viewer",  # Domain sharing -> viewer
                        )
                    )

            elif perm_type == "anyone":
                if not self._block_anyone:
                    entries.append(
                        PermissionEntry(
                            subject_type="domain",
                            subject_id="*",
                            relation="viewer",
                        )
                    )
                else:
                    logger.debug("Blocked 'anyone' permission on %s", doc_id)

        return entries

    async def get_folder_permissions(self, folder_id: str) -> list[PermissionEntry]:
        """Drive folder permissions propagate to contents."""
        return await self.get_permissions(folder_id)

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/about", params={"fields": "user"})
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _parse_timestamp(ts: str) -> datetime:
    """Parse Google API RFC 3339 timestamp."""
    if not ts:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
