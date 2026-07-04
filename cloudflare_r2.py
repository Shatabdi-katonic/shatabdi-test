"""Cloudflare R2 connector.

API: S3-compatible (ListObjectsV2 / GetObject)
Auth: Cloudflare account_id + R2 access_key + secret_key
Sync: Full listing with LastModified filter for incremental
Permissions: R2 uses Cloudflare IAM, not per-object ACLs -- returns empty

Cloudflare R2 provides an S3-compatible API at
{account_id}.r2.cloudflarestorage.com. This connector uses raw HTTP
with AWS Signature V4 to avoid requiring boto3.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import quote
from xml.etree import ElementTree

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

# S3 XML namespace
S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"

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
    """Infer MIME type from object name extension."""
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
        return EXTENSION_MIMES.get(ext, "application/octet-stream")
    return "application/octet-stream"


class CloudflareR2Connector(ConnectorBase):
    """Native Cloudflare R2 connector using S3-compatible API.

    Config:
        bucket: Bucket name (required).
        prefix: Object name prefix to scope the sync. Default "".
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # CR-609: the Add-Knowledge wizard sends `r2Bucket`/`r2Prefix`
        # (knowledgeProviders.js), not `bucket`/`prefix`. Accept both, since the
        # inline-sync path merges source.config into the connector config.
        self._bucket: str = config.get("bucket") or config.get("r2Bucket") or ""
        self._prefix: str = config.get("prefix") or config.get("r2Prefix") or ""
        self._account_id: str = ""
        self._access_key: str = ""
        self._secret_key: str = ""
        self._client: RetryClient | None = None

    def _endpoint(self) -> str:
        return f"https://{self._account_id}.r2.cloudflarestorage.com"

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with Cloudflare R2 credentials.

        Expected credentials: {
            account_id: str,   # Cloudflare account ID
            access_key: str,   # R2 access key ID
            secret_key: str,   # R2 secret access key
        }
        """
        # CR-609: the wizard sends `r2AccountId`/`r2AccessKey`/`r2SecretKey`;
        # inline sync merges source.config into `credentials`, so accept those
        # keys as fallbacks to the canonical snake_case names. Without this every
        # field was empty → "Missing R2 credentials" → 0 docs even when entered.
        self._account_id = credentials.get("account_id") or credentials.get("r2AccountId") or ""
        self._access_key = credentials.get("access_key") or credentials.get("r2AccessKey") or ""
        self._secret_key = credentials.get("secret_key") or credentials.get("r2SecretKey") or ""
        # Bucket/prefix usually arrive via config, but the merged sync payload may
        # surface them here too — backfill if __init__ didn't see them.
        self._bucket = self._bucket or credentials.get("bucket") or credentials.get("r2Bucket") or ""
        self._prefix = self._prefix or credentials.get("prefix") or credentials.get("r2Prefix") or ""

        if not all([self._account_id, self._access_key, self._secret_key]):
            raise ConnectorAuthError(
                "Missing R2 credentials (account_id, access_key, secret_key required)",
                connector_type="cloudflare_r2",
            )
        if not self._bucket:
            raise ConnectorAuthError(
                "Bucket name is required in config",
                connector_type="cloudflare_r2",
            )

        self._client = RetryClient(
            base_url=self._endpoint(),
            timeout=60.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify access with a HEAD bucket request
        try:
            headers = self._sign_request("HEAD", f"/{self._bucket}")
            resp = await self._client.request(
                "HEAD", f"/{self._bucket}", headers=headers
            )
            logger.info(
                "Cloudflare R2 authenticated, bucket=%s prefix=%s",
                self._bucket,
                self._prefix,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ConnectorAuthError(
                    "R2 authentication failed -- check credentials",
                    connector_type="cloudflare_r2",
                ) from exc
            _raise_for_status(exc)
            raise

    def _sign_request(
        self,
        method: str,
        path: str,
        query_params: dict[str, str] | None = None,
        payload_hash: str = "UNSIGNED-PAYLOAD",
    ) -> dict[str, str]:
        """Generate AWS Signature V4 headers for an S3-compatible request.

        R2 uses the 'auto' region for signing.
        """
        now = datetime.now(UTC)
        datestamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        region = "auto"
        service = "s3"

        host = f"{self._account_id}.r2.cloudflarestorage.com"

        # Canonical query string
        if query_params:
            sorted_params = sorted(query_params.items())
            canonical_qs = "&".join(
                f"{quote(k, safe='')}={quote(v, safe='')}"
                for k, v in sorted_params
            )
        else:
            canonical_qs = ""

        # Canonical headers
        canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        signed_headers = "host;x-amz-content-sha256;x-amz-date"

        canonical_request = (
            f"{method}\n{path}\n{canonical_qs}\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )

        # String to sign
        credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )

        # Signing key
        def _hmac_sha256(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k_date = _hmac_sha256(f"AWS4{self._secret_key}".encode(), datestamp)
        k_region = _hmac_sha256(k_date, region)
        k_service = _hmac_sha256(k_region, service)
        k_signing = _hmac_sha256(k_service, "aws4_request")

        signature = hmac.new(
            k_signing, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()

        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        return {
            "Host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": authorization,
        }

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List objects in the R2 bucket using ListObjectsV2.

        Paginates via ContinuationToken. Filters by LastModified if since
        is provided.
        """
        assert self._client is not None

        continuation_token: str | None = None

        while True:
            params: dict[str, str] = {
                "list-type": "2",
                "max-keys": "1000",
            }
            if self._prefix:
                params["prefix"] = self._prefix
            if continuation_token:
                params["continuation-token"] = continuation_token

            headers = self._sign_request(
                "GET", f"/{self._bucket}", query_params=params
            )

            try:
                resp = await self._client.get(
                    f"/{self._bucket}", headers=headers, params=params
                )
            except httpx.HTTPStatusError as exc:
                _raise_for_status(exc)
                raise

            root = ElementTree.fromstring(resp.text)

            for contents in root.findall(f"{{{S3_NS}}}Contents"):
                key_el = contents.find(f"{{{S3_NS}}}Key")
                key = key_el.text if key_el is not None and key_el.text else ""

                if not key or key.endswith("/"):
                    continue

                last_mod_el = contents.find(f"{{{S3_NS}}}LastModified")
                if last_mod_el is not None and last_mod_el.text:
                    modified = datetime.fromisoformat(
                        last_mod_el.text.replace("Z", "+00:00")
                    )
                else:
                    modified = datetime.now(UTC)

                if since and modified < since:
                    continue

                size_el = contents.find(f"{{{S3_NS}}}Size")
                size = int(size_el.text) if size_el is not None and size_el.text else None

                filename = key.rsplit("/", 1)[-1]

                yield DocumentMetadata(
                    external_id=key,
                    title=filename,
                    url=f"r2://{self._bucket}/{key}",
                    content_type=_ext_mime(key),
                    size_bytes=size,
                    modified_at=modified,
                    folder_id=key.rsplit("/", 1)[0] if "/" in key else "",
                    metadata={
                        "bucket": self._bucket,
                        "account_id": self._account_id,
                    },
                )

            # Check for pagination
            is_truncated_el = root.find(f"{{{S3_NS}}}IsTruncated")
            is_truncated = (
                is_truncated_el is not None
                and is_truncated_el.text
                and is_truncated_el.text.lower() == "true"
            )

            if not is_truncated:
                break

            next_token_el = root.find(f"{{{S3_NS}}}NextContinuationToken")
            if next_token_el is not None and next_token_el.text:
                continuation_token = next_token_el.text
            else:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Download an object from R2 using GetObject."""
        assert self._client is not None

        encoded_key = quote(doc_id, safe="/")
        path = f"/{self._bucket}/{encoded_key}"
        headers = self._sign_request("GET", path)

        try:
            resp = await self._client.get(path, headers=headers)
        except httpx.HTTPStatusError as exc:
            _raise_for_status(exc)
            raise

        content_type = resp.headers.get("content-type", _ext_mime(doc_id))

        return RawDocument(
            external_id=doc_id,
            content=resp.content,
            content_type=content_type,
            metadata={
                "bucket": self._bucket,
                "account_id": self._account_id,
            },
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """R2 uses Cloudflare IAM for access control, not per-object ACLs.

        Returns an empty list.
        """
        return []

    async def health_check(self) -> bool:
        """Verify R2 connectivity."""
        if self._client is None:
            return False
        try:
            headers = self._sign_request("HEAD", f"/{self._bucket}")
            await self._client.request("HEAD", f"/{self._bucket}", headers=headers)
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
            f"R2 authentication/authorization failed ({status})",
            connector_type="cloudflare_r2",
        ) from exc
    if status == 429:
        retry_after = float(exc.response.headers.get("Retry-After", "5"))
        raise ConnectorRateLimitError(
            "R2 rate limit exceeded",
            connector_type="cloudflare_r2",
            retry_after=retry_after,
        ) from exc
    if status >= 500:
        raise ConnectorTransientError(
            f"R2 server error {status}", connector_type="cloudflare_r2"
        ) from exc
