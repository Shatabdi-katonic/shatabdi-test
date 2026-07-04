"""Oracle Cloud Infrastructure Object Storage connector.

API: OCI Object Storage REST API
Auth: OCI API key signing (tenancy, user, fingerprint, private key)
Sync: Full listing with timeModified filter for incremental
Permissions: OCI uses IAM, not per-object ACLs -- returns empty permissions

Uses the oci Python SDK for request signing when available,
falling back to manual HTTP signature construction.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import quote

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


class OracleStorageConnector(ConnectorBase):
    """Native Oracle Cloud Infrastructure Object Storage connector.

    Config:
        region: OCI region (e.g. "us-phoenix-1"). Required.
        namespace: Object Storage namespace. Required.
        bucket: Bucket name. Required.
        prefix: Object name prefix to scope the sync. Default "".
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._region: str = config.get("region", "")
        self._namespace: str = config.get("namespace", "")
        self._bucket: str = config.get("bucket", "")
        self._prefix: str = config.get("prefix", "")
        self._client: RetryClient | None = None
        self._signer = None

    def _base_url(self) -> str:
        return f"https://objectstorage.{self._region}.oraclecloud.com"

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with OCI API key credentials.

        Expected credentials: {
            tenancy: str,       # OCI tenancy OCID
            user: str,          # User OCID
            fingerprint: str,   # API key fingerprint
            private_key: str,   # PEM-encoded private key
            region: str,        # Optional, overrides config region
        }
        """
        tenancy = credentials.get("tenancy")
        user = credentials.get("user")
        fingerprint = credentials.get("fingerprint")
        private_key = credentials.get("private_key")

        if not all([tenancy, user, fingerprint, private_key]):
            raise ConnectorAuthError(
                "Missing OCI credentials (tenancy, user, fingerprint, private_key required)",
                connector_type="oracle_storage",
            )

        # Allow region override from credentials
        if credentials.get("region"):
            self._region = credentials["region"]

        if not self._region:
            raise ConnectorAuthError(
                "OCI region is required", connector_type="oracle_storage"
            )
        if not self._namespace:
            raise ConnectorAuthError(
                "OCI namespace is required in config", connector_type="oracle_storage"
            )
        if not self._bucket:
            raise ConnectorAuthError(
                "OCI bucket is required in config", connector_type="oracle_storage"
            )

        try:
            import oci

            config = {
                "tenancy": tenancy,
                "user": user,
                "fingerprint": fingerprint,
                "key_content": private_key,
                "region": self._region,
            }
            oci.config.validate_config(config)
            self._signer = oci.signer.Signer(
                tenancy=tenancy,
                user=user,
                fingerprint=fingerprint,
                private_key_content=private_key,
            )
            logger.info("OCI authenticated using oci SDK signer")
        except ImportError:
            # Fall back to manual signing
            self._signer = _ManualOCISigner(
                tenancy=tenancy,
                user=user,
                fingerprint=fingerprint,
                private_key_pem=private_key,
            )
            logger.info("OCI authenticated using manual HTTP signature signer")

        self._client = RetryClient(
            base_url=self._base_url(),
            timeout=60.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify access by listing with limit=1
        try:
            url = f"/n/{self._namespace}/b/{self._bucket}/o"
            headers = self._sign_headers("GET", url)
            await self._client.get(url, headers=headers, params={"limit": "1"})
            logger.info(
                "OCI Object Storage connected: namespace=%s bucket=%s",
                self._namespace,
                self._bucket,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ConnectorAuthError(
                    "OCI authentication failed -- check credentials and policies",
                    connector_type="oracle_storage",
                ) from exc
            _raise_for_status(exc)
            raise

    def _sign_headers(self, method: str, path: str) -> dict[str, str]:
        """Generate signed headers for an OCI API request."""
        if self._signer is None:
            return {}

        try:
            import oci  # noqa: F811

            # oci SDK signer: create a fake request and sign it
            import requests as req_lib

            fake_req = req_lib.Request(
                method=method, url=self._base_url() + path
            ).prepare()
            self._signer(fake_req)
            return dict(fake_req.headers)
        except (ImportError, TypeError):
            # Manual signer
            if isinstance(self._signer, _ManualOCISigner):
                return self._signer.sign(method, self._base_url() + path)
            return {}

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List objects in the OCI bucket.

        Uses the ListObjects API with pagination via nextStartWith.
        """
        assert self._client is not None

        base_path = f"/n/{self._namespace}/b/{self._bucket}/o"
        start: str | None = None

        while True:
            params: dict[str, str] = {"limit": "1000"}
            if self._prefix:
                params["prefix"] = self._prefix
            if start:
                params["start"] = start

            headers = self._sign_headers("GET", base_path)

            try:
                resp = await self._client.get(
                    base_path, headers=headers, params=params
                )
            except httpx.HTTPStatusError as exc:
                _raise_for_status(exc)
                raise

            data = resp.json()

            for obj in data.get("objects", []):
                name: str = obj.get("name", "")

                if name.endswith("/"):
                    continue

                # OCI returns timeModified as ISO 8601
                time_modified = obj.get("timeModified", "")
                if time_modified:
                    modified = datetime.fromisoformat(
                        time_modified.replace("Z", "+00:00")
                    )
                else:
                    modified = datetime.now(UTC)

                if since and modified < since:
                    continue

                size = obj.get("size")
                filename = name.rsplit("/", 1)[-1]

                yield DocumentMetadata(
                    external_id=name,
                    title=filename,
                    url=f"oci://{self._bucket}/{name}",
                    content_type=_ext_mime(name),
                    size_bytes=size,
                    modified_at=modified,
                    folder_id=name.rsplit("/", 1)[0] if "/" in name else "",
                    metadata={
                        "namespace": self._namespace,
                        "bucket": self._bucket,
                        "md5": obj.get("md5", ""),
                    },
                )

            next_start = data.get("nextStartWith")
            if not next_start:
                break
            start = next_start

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Download an object from OCI Object Storage."""
        assert self._client is not None

        encoded = quote(doc_id, safe="")
        path = f"/n/{self._namespace}/b/{self._bucket}/o/{encoded}"
        headers = self._sign_headers("GET", path)

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
                "namespace": self._namespace,
                "bucket": self._bucket,
            },
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """OCI uses IAM for access control, not per-object ACLs.

        Returns an empty list.
        """
        return []

    async def health_check(self) -> bool:
        """Verify OCI Object Storage connectivity."""
        if self._client is None:
            return False
        try:
            path = f"/n/{self._namespace}/b/{self._bucket}/o"
            headers = self._sign_headers("GET", path)
            await self._client.get(path, headers=headers, params={"limit": "1"})
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """Release HTTP client resources."""
        if self._client:
            await self._client.close()


class _ManualOCISigner:
    """Minimal OCI HTTP signature signer for when the oci SDK is not installed.

    Implements the OCI request signing spec using RSA-SHA256.
    """

    def __init__(
        self, tenancy: str, user: str, fingerprint: str, private_key_pem: str
    ) -> None:
        self._key_id = f"{tenancy}/{user}/{fingerprint}"
        self._private_key_pem = private_key_pem

    def sign(self, method: str, url: str) -> dict[str, str]:
        """Generate OCI-signed headers for a request."""
        import base64
        import hashlib
        from datetime import timezone
        from email.utils import formatdate
        from urllib.parse import urlparse

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        parsed = urlparse(url)
        date_str = formatdate(usegmt=True)
        host = parsed.hostname or ""
        target = f"{method.lower()} {parsed.path}"
        if parsed.query:
            target += f"?{parsed.query}"

        headers_to_sign = [
            ("date", date_str),
            ("(request-target)", target),
            ("host", host),
        ]

        signing_string = "\n".join(f"{k}: {v}" for k, v in headers_to_sign)
        header_names = " ".join(k for k, _ in headers_to_sign)

        private_key = serialization.load_pem_private_key(
            self._private_key_pem.encode(), password=None
        )
        signature = private_key.sign(
            signing_string.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode()

        auth_header = (
            f'Signature version="1",'
            f'keyId="{self._key_id}",'
            f'algorithm="rsa-sha256",'
            f'headers="{header_names}",'
            f'signature="{sig_b64}"'
        )

        return {
            "date": date_str,
            "host": host,
            "authorization": auth_header,
        }


def _raise_for_status(exc: httpx.HTTPStatusError) -> None:
    """Convert HTTP errors to connector-specific exceptions."""
    status = exc.response.status_code
    if status in (401, 403):
        raise ConnectorAuthError(
            f"OCI authentication/authorization failed ({status})",
            connector_type="oracle_storage",
        ) from exc
    if status == 429:
        retry_after = float(exc.response.headers.get("Retry-After", "5"))
        raise ConnectorRateLimitError(
            "OCI rate limit exceeded",
            connector_type="oracle_storage",
            retry_after=retry_after,
        ) from exc
    if status >= 500:
        raise ConnectorTransientError(
            f"OCI server error {status}", connector_type="oracle_storage"
        ) from exc
