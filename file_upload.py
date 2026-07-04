"""File upload connector.

Handles direct file uploads (not connected to any external source).
Documents are stored directly in MinIO. No OAuth, no sync schedule.
This is the simplest path through the pipeline and useful for testing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from platform_knowledge_engine.connectors.base import (
    ConnectorBase,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

# Map file extensions to MIME types for config-based files
_EXT_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".rst": "text/x-rst",
}


def _guess_content_type(filename: str) -> str:
    """Guess MIME type from filename extension."""
    ext = os.path.splitext(filename)[1].lower()
    return _EXT_TO_MIME.get(ext, "application/octet-stream")


class FileUploadConnector(ConnectorBase):
    """Connector for direct file uploads.

    Unlike other connectors, this doesn't poll an external system.
    Documents are pushed in via the upload endpoint and stored in MinIO.
    Permissions default to the uploading user as owner.

    When instantiated by the sync workflow, reads the file list from
    source config["files"] (persisted in PostgreSQL) so that previously
    uploaded files are discoverable even though the in-memory state is gone.
    """

    def __init__(self, config: dict | None = None) -> None:
        self._documents: dict[str, RawDocument] = {}
        self._metadata: dict[str, DocumentMetadata] = {}
        self._uploading_user: str | None = None
        self._config = config or {}

    async def authenticate(self, credentials: dict) -> None:
        """No authentication needed for file uploads."""
        self._uploading_user = credentials.get("user_id")

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List uploaded documents.

        First yields any in-memory documents (from current upload session),
        then yields documents from config["files"] (persisted from prior uploads).
        This ensures the sync workflow can discover files that were uploaded
        in a previous request.
        """
        yielded_ids: set[str] = set()

        # 1. In-memory documents (current upload session)
        for meta in self._metadata.values():
            if since is None or meta.modified_at >= since:
                yielded_ids.add(meta.external_id)
                yield meta

        # 2. Config-persisted files (from prior uploads / legacy sources)
        config_files = self._config.get("files") or []
        for f in config_files:
            file_name = f.get("fileName") or f.get("name") or ""
            file_path = f.get("filePath") or f.get("path") or ""
            file_size = f.get("fileSize") or f.get("size_bytes") or 0
            # Use filePath as stable external_id so we don't create duplicates
            external_id = file_path or file_name
            if not external_id or external_id in yielded_ids:
                continue
            yielded_ids.add(external_id)
            yield DocumentMetadata(
                external_id=external_id,
                title=file_name,
                content_type=_guess_content_type(file_name),
                size_bytes=int(file_size) if file_size else 0,
                modified_at=datetime.now(UTC),
            )

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch uploaded document bytes.

        Checks in-memory documents first (current upload session), then
        falls back to reading from the filesystem path stored in config.
        This handles legacy sources whose files live on NFS mounts.
        """
        # 1. In-memory (current upload session)
        if doc_id in self._documents:
            return self._documents[doc_id]

        # 2. Config-persisted files — doc_id is the filePath used as external_id
        config_files = self._config.get("files") or []
        for f in config_files:
            file_name = f.get("fileName") or f.get("name") or ""
            file_path = f.get("filePath") or f.get("path") or ""
            external_id = file_path or file_name
            if external_id != doc_id:
                continue

            # Found matching config entry — read from filesystem
            read_path = file_path or file_name
            p = Path(read_path).resolve()

            # Path traversal protection: reject symlinks and paths outside allowed roots.
            # Default roots include /data, /tmp, /datasets, and /mnt/nfs-mounts
            # (the Kubernetes volume where uploaded files are stored).
            # Override via FILE_UPLOAD_ALLOWED_ROOTS env var (colon-separated paths).
            _default_roots = [Path("/data"), Path("/tmp"), Path("/datasets"), Path("/mnt/nfs-mounts")]
            _env_roots_str = os.environ.get("FILE_UPLOAD_ALLOWED_ROOTS", "")
            _env_roots = [Path(r.strip()) for r in _env_roots_str.split(":") if r.strip()]
            _ALLOWED_ROOTS = tuple(_default_roots + _env_roots)
            if p.is_symlink():
                raise PermissionError(f"Symlinks not allowed: {read_path}")
            if not any(p.is_relative_to(root) for root in _ALLOWED_ROOTS):
                raise PermissionError(
                    f"Path {read_path} is outside allowed directories. "
                    f"Files must be under {', '.join(str(r) for r in _ALLOWED_ROOTS)}."
                )

            if not p.is_file():
                raise FileNotFoundError(
                    f"File not found on disk: {read_path} "
                    f"(source config references this path but it is not mounted)"
                )

            content = p.read_bytes()
            return RawDocument(
                external_id=doc_id,
                content=content,
                content_type=_guess_content_type(file_name),
                metadata={"original_filename": file_name, "source_path": read_path},
            )

        raise FileNotFoundError(f"Document {doc_id} not found in memory or config")

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Uploaded files are owned by the uploader."""
        if self._uploading_user:
            return [
                PermissionEntry(
                    subject_type="user",
                    subject_id=self._uploading_user,
                    relation="owner",
                )
            ]
        return []

    def register_upload(
        self,
        filename: str,
        content: bytes,
        content_type: str,
        user_id: str,
    ) -> str:
        """Register a file upload. Returns the document external_id."""
        external_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        self._metadata[external_id] = DocumentMetadata(
            external_id=external_id,
            title=filename,
            content_type=content_type,
            size_bytes=len(content),
            modified_at=now,
        )

        self._documents[external_id] = RawDocument(
            external_id=external_id,
            content=content,
            content_type=content_type,
            metadata={"original_filename": filename},
        )

        self._uploading_user = user_id
        return external_id
