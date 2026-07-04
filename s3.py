"""S3 / GCS / Azure Blob connector.

API: AWS S3 SDK (boto3) -- also works with S3-compatible stores (MinIO, GCS interop)
Auth: Access key + secret key, or IAM role
Sync: Full listing (S3 has no incremental API; use LastModified filter)
Permissions: Bucket-level (all users with source access can read)

S3 doesn't have per-object ACLs in a way that maps to user/group permissions.
The permission model here is: if you have access to the knowledge source,
you have access to all documents in it. Fine-grained permissions are handled
at the source level in the platform admin UI.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors.base import (
    ConnectorBase,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)

# Content type inference by extension
EXTENSION_MIMES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".rtf": "application/rtf",
    ".epub": "application/epub+zip",
}


class S3Connector(ConnectorBase):
    """S3-compatible object store connector.

    Config:
        bucket: Bucket name (required).
        prefix: Key prefix to scope the sync. Default "" (entire bucket).
        endpoint_url: Custom endpoint for MinIO/GCS. Default None (AWS S3).
        region: AWS region. Default "us-east-1".
        file_extensions: List of extensions to include. Default all supported.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._bucket: str = config.get("bucket", "")
        self._prefix: str = config.get("prefix", "")
        self._endpoint_url: str | None = config.get("endpoint_url")
        self._region: str = config.get("region", "us-east-1")
        self._extensions: set[str] = set(config.get("file_extensions", EXTENSION_MIMES.keys()))
        self._s3_client = None

    async def authenticate(self, credentials: dict) -> None:
        import boto3

        kwargs: dict = {
            "service_name": "s3",
            "region_name": self._region,
        }
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url

        if "access_key" in credentials:
            kwargs["aws_access_key_id"] = credentials["access_key"]
            kwargs["aws_secret_access_key"] = credentials["secret_key"]

        self._s3_client = boto3.client(**kwargs)

        # Verify access
        await asyncio.to_thread(
            self._s3_client.head_bucket,
            Bucket=self._bucket,
        )
        logger.info("S3 authenticated, bucket=%s prefix=%s", self._bucket, self._prefix)

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        assert self._s3_client is not None

        paginator = self._s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=self._bucket, Prefix=self._prefix)

        # Process one page at a time to avoid OOM on large buckets
        # (previously used list(pages) which materialized entire paginator)
        def _next_page(page_iter):
            try:
                return next(page_iter)
            except StopIteration:
                return None

        page_iter = iter(await asyncio.to_thread(lambda: pages))
        while True:
            page = await asyncio.to_thread(_next_page, page_iter)
            if page is None:
                break
            for obj in page.get("Contents", []):
                key = obj["Key"]

                # Skip directories
                if key.endswith("/"):
                    continue

                # Filter by extension
                ext = _get_extension(key)
                if self._extensions and ext not in self._extensions:
                    continue

                modified = obj.get("LastModified", datetime.now(UTC))
                if since and modified < since:
                    continue

                content_type = EXTENSION_MIMES.get(ext, "application/octet-stream")
                filename = key.rsplit("/", 1)[-1]

                yield DocumentMetadata(
                    external_id=key,
                    title=filename,
                    url=f"s3://{self._bucket}/{key}",
                    content_type=content_type,
                    size_bytes=obj.get("Size"),
                    modified_at=modified,
                    metadata={"bucket": self._bucket, "key": key},
                )

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Download object from S3."""
        assert self._s3_client is not None

        resp = await asyncio.to_thread(
            self._s3_client.get_object,
            Bucket=self._bucket,
            Key=doc_id,
        )
        content = await asyncio.to_thread(resp["Body"].read)
        content_type = resp.get("ContentType", "application/octet-stream")

        # Override content type from extension if generic
        if content_type == "application/octet-stream" or content_type == "binary/octet-stream":
            ext = _get_extension(doc_id)
            content_type = EXTENSION_MIMES.get(ext, content_type)

        return RawDocument(
            external_id=doc_id,
            content=content,
            content_type=content_type,
            metadata={"bucket": self._bucket},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """S3 uses source-level permissions. No per-object ACL extraction."""
        return []

    async def health_check(self) -> bool:
        if self._s3_client is None:
            return False
        try:
            await asyncio.to_thread(
                self._s3_client.head_bucket,
                Bucket=self._bucket,
            )
            return True
        except Exception:
            return False

    async def close(self) -> None:
        pass


def _get_extension(key: str) -> str:
    """Extract lowercase file extension from S3 key."""
    if "." in key:
        return "." + key.rsplit(".", 1)[-1].lower()
    return ""
