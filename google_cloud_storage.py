"""Google Cloud Storage connector.

API: GCS JSON API v1
Auth: Service account JSON (google-auth library)
Sync: Full listing with lastModified filter for incremental
Permissions: GCS uses IAM, not per-object ACLs -- returns empty permissions

Objects are listed from a single bucket with an optional prefix filter.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

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

GCS_API = "https://storage.googleapis.com/storage/v1"
GCS_UPLOAD_SCOPES = ["https://www.googleapis.com/auth/devstorage.read_only"]

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
    ".xml": "application/xml",
}


def _ext_mime(name: str) -> str:
    """Infer MIME type from object name extension."""
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
        return EXTENSION_MIMES.get(ext, "application/octet-stream")
    return "application/octet-stream"


class GoogleCloudStorageConnector(ConnectorBase):
    """Native Google Cloud Storage connector using GCS JSON API.

    Config:
        bucket: Bucket name (required).
        prefix: Object name prefix to scope the sync. Default "".
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._bucket: str = config.get("bucket", "")
        self._prefix: str = config.get("prefix", "")
        self._client: RetryClient | None = None
        self._credentials = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with a GCP service account JSON key.

        Expected credentials: {service_account_json: dict}
        The service_account_json should be the parsed JSON key file contents.
        """
        sa_json = credentials.get("service_account_json")
        if not sa_json:
            raise ConnectorAuthError(
                "Missing service_account_json", connector_type="gcs"
            )

        if not self._bucket:
            raise ConnectorAuthError(
                "Bucket name is required in config", connector_type="gcs"
            )

        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            self._credentials = service_account.Credentials.from_service_account_info(
                sa_json, scopes=GCS_UPLOAD_SCOPES
            )
            # Force an initial token refresh
            self._credentials.refresh(Request())
        except Exception as exc:
            raise ConnectorAuthError(
                f"Failed to authenticate with GCS service account: {exc}",
                connector_type="gcs",
            ) from exc

        self._client = RetryClient(
            base_url=GCS_API,
            headers={"Authorization": f"Bearer {self._credentials.token}"},
            timeout=60.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify bucket access
        try:
            await self._client.get(f"/b/{self._bucket}", params={"fields": "name"})
            logger.info(
                "GCS authenticated, bucket=%s prefix=%s", self._bucket, self._prefix
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ConnectorAuthError(
                    f"No access to bucket {self._bucket}", connector_type="gcs"
                ) from exc
            _raise_for_status(exc)
            raise

    async def _refresh_token_if_needed(self) -> None:
        """Refresh the OAuth token if expired or about to expire (5 min buffer)."""
        if self._credentials is None or self._client is None:
            return
        # Proactive refresh: also refresh if within 5 minutes of expiry
        # to avoid mid-request failures on large file downloads
        import time
        expiry = getattr(self._credentials, "expiry", None)
        needs_refresh = not self._credentials.valid
        if not needs_refresh and expiry:
            remaining = (expiry.timestamp() - time.time()) if hasattr(expiry, "timestamp") else 0
            needs_refresh = remaining < 300  # 5 minutes
        if needs_refresh:
            from google.auth.transport.requests import Request

            self._credentials.refresh(Request())
            self._client._client.headers["Authorization"] = (
                f"Bearer {self._credentials.token}"
            )
            logger.debug("GCS token refreshed")

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List objects in the GCS bucket with optional prefix filter.

        Uses the objects.list endpoint with pageToken pagination.
        Filters by timeCreated/updated if since is provided.
        """
        assert self._client is not None
        await self._refresh_token_if_needed()

        params: dict = {
            "maxResults": "1000",
            "fields": "items(name,size,contentType,updated,timeCreated,metadata),nextPageToken",
        }
        if self._prefix:
            params["prefix"] = self._prefix

        page_token: str | None = None

        while True:
            if page_token:
                params["pageToken"] = page_token

            try:
                resp = await self._client.get(
                    f"/b/{self._bucket}/o", params=params
                )
            except httpx.HTTPStatusError as exc:
                _raise_for_status(exc)
                raise

            data = resp.json()

            for obj in data.get("items", []):
                name: str = obj.get("name", "")

                # Skip "directory" markers
                if name.endswith("/"):
                    continue

                updated_str = obj.get("updated", "")
                if updated_str:
                    modified = datetime.fromisoformat(
                        updated_str.replace("Z", "+00:00")
                    )
                else:
                    modified = datetime.now(UTC)

                if since and modified < since:
                    continue

                size_str = obj.get("size")
                size = int(size_str) if size_str else None
                content_type = obj.get("contentType") or _ext_mime(name)
                filename = name.rsplit("/", 1)[-1]

                yield DocumentMetadata(
                    external_id=name,
                    title=filename,
                    url=f"gs://{self._bucket}/{name}",
                    content_type=content_type,
                    size_bytes=size,
                    modified_at=modified,
                    folder_id=name.rsplit("/", 1)[0] if "/" in name else "",
                    metadata={
                        "bucket": self._bucket,
                        "object_name": name,
                    },
                )

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Download an object from GCS by its object name.

        Uses alt=media to get raw bytes.
        """
        assert self._client is not None
        await self._refresh_token_if_needed()

        # URL-encode the object name (slashes are part of the key)
        from urllib.parse import quote

        encoded = quote(doc_id, safe="")

        try:
            resp = await self._client.get(
                f"/b/{self._bucket}/o/{encoded}",
                params={"alt": "media"},
            )
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc)
            raise

        content_type = resp.headers.get("content-type", _ext_mime(doc_id))

        return RawDocument(
            external_id=doc_id,
            content=resp.content,
            content_type=content_type,
            metadata={"bucket": self._bucket},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """GCS uses IAM for access control, not per-object ACLs.

        Returns an empty list. Access is governed at the bucket/project level.
        """
        return []

    async def health_check(self) -> bool:
        """Verify connectivity to the GCS bucket."""
        if self._client is None:
            return False
        try:
            await self._refresh_token_if_needed()
            await self._client.get(f"/b/{self._bucket}", params={"fields": "name"})
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
            f"GCS authentication/authorization failed ({status})",
            connector_type="gcs",
        ) from exc
    if status == 429:
        retry_after = float(exc.response.headers.get("Retry-After", "5"))
        raise ConnectorRateLimitError(
            "GCS rate limit exceeded",
            connector_type="gcs",
            retry_after=retry_after,
        ) from exc
    if status >= 500:
        raise ConnectorTransientError(
            f"GCS server error {status}", connector_type="gcs"
        ) from exc
