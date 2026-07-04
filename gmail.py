"""Gmail connector.

API: Gmail API v1 (messages.list, messages.get)
Auth: Google service account (service_account_json with domain-wide delegation)
Sync: Incremental (after:{epoch} query) + full
Permissions: Empty (personal inbox; no shared permissions model)
"""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers

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

GMAIL_API = "https://gmail.googleapis.com"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailConnector(ConnectorBase):
    """Native Gmail connector via Gmail API with service account auth.

    Config:
        user_email: The email address to impersonate (domain-wide delegation).
        max_results: Page size for message listing. Default 100.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._user_email: str = config.get("user_email", "me")
        self._max_results: int = config.get("max_results", 100)
        self._client: httpx.AsyncClient | None = None
        self._access_token: str = ""

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using a Google service account JSON key.

        Expects credentials with: service_account_json (dict).
        Uses google-auth library for JWT signing and token exchange.
        """
        sa_json = credentials.get("service_account_json") or credentials.get("gmailCredentials")
        if not sa_json:
            raise ConnectorAuthError(
                "Gmail connector requires service_account_json", connector_type="gmail"
            )

        try:
            from google.oauth2 import service_account as sa_module

            creds = sa_module.Credentials.from_service_account_info(
                sa_json, scopes=SCOPES
            )
            if self._user_email and self._user_email != "me":
                creds = creds.with_subject(self._user_email)

            # Perform token refresh to obtain access_token
            from google.auth.transport.requests import Request

            creds.refresh(Request())
            self._access_token = creds.token
        except Exception as e:
            raise ConnectorAuthError(
                f"Gmail service account auth failed: {e}", connector_type="gmail"
            ) from e

        if not self._access_token:
            raise ConnectorAuthError(
                "No access token obtained from service account",
                connector_type="gmail",
            )

        self._client = RetryClient(
            base_url=GMAIL_API,
            headers=bearer_headers(self._access_token),
            timeout=30.0,
            rate_limiter=self.rate_limiter,
        )
        logger.info("Gmail connector authenticated for %s", self._user_email)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET with standard error handling."""
        assert self._client is not None
        try:
            resp = await self._client.get(url, params=params)
        except httpx.TimeoutException as e:
            raise ConnectorTransientError(
                f"Gmail API timeout: {e}", connector_type="gmail"
            ) from e

        if resp.status_code == 401:
            raise ConnectorAuthError(
                "Gmail token expired or invalid", connector_type="gmail"
            )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            raise ConnectorRateLimitError(
                "Gmail API rate limited",
                connector_type="gmail",
                retry_after=retry_after,
            )
        if resp.status_code >= 500:
            raise ConnectorTransientError(
                f"Gmail API server error: {resp.status_code}", connector_type="gmail"
            )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _decode_body(payload: dict) -> str:
        """Recursively extract plain-text body from Gmail message payload."""
        mime = payload.get("mimeType", "")

        if mime == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        # Multipart: recurse into parts, prefer text/plain
        parts = payload.get("parts", [])
        for part in parts:
            text = GmailConnector._decode_body(part)
            if text:
                return text

        return ""

    @staticmethod
    def _get_header(headers: list[dict], name: str) -> str:
        """Extract a header value by name from Gmail headers list."""
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    # ------------------------------------------------------------------
    # ConnectorBase implementation
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List Gmail messages, optionally filtered by date.

        Uses the ``after:{epoch}`` search query for incremental sync.
        Paginates through all results using ``nextPageToken``.
        """
        assert self._client is not None

        params: dict = {"maxResults": str(self._max_results)}
        if since:
            epoch = int(since.timestamp())
            params["q"] = f"after:{epoch}"

        page_token: str | None = None
        while True:
            if page_token:
                params["pageToken"] = page_token

            data = await self._get_json(
                "/gmail/v1/users/me/messages", params=params
            )

            for msg_stub in data.get("messages", []):
                msg_id = msg_stub["id"]

                # Fetch minimal metadata for listing
                meta = await self._get_json(
                    f"/gmail/v1/users/me/messages/{msg_id}",
                    params={"format": "metadata", "metadataHeaders": "Subject,From,Date"},
                )

                headers = meta.get("payload", {}).get("headers", [])
                subject = self._get_header(headers, "Subject") or "(no subject)"
                sender = self._get_header(headers, "From")
                date_str = self._get_header(headers, "Date")

                modified = datetime.now(UTC)
                if date_str:
                    try:
                        modified = parsedate_to_datetime(date_str)
                        if modified.tzinfo is None:
                            modified = modified.replace(tzinfo=UTC)
                    except Exception:
                        pass

                yield DocumentMetadata(
                    external_id=msg_id,
                    title=subject,
                    url=f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
                    content_type="text/plain",
                    author=sender,
                    modified_at=modified,
                    size_bytes=int(meta.get("sizeEstimate", 0)),
                    metadata={
                        "thread_id": meta.get("threadId", ""),
                        "label_ids": meta.get("labelIds", []),
                        "snippet": meta.get("snippet", ""),
                    },
                )

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a full Gmail message and decode the body.

        Returns the message formatted as: Subject, From, Date, then body text.
        """
        assert self._client is not None

        data = await self._get_json(
            f"/gmail/v1/users/me/messages/{doc_id}", params={"format": "full"}
        )

        payload = data.get("payload", {})
        headers = payload.get("headers", [])
        subject = self._get_header(headers, "Subject") or "(no subject)"
        sender = self._get_header(headers, "From")
        date_str = self._get_header(headers, "Date")

        body = self._decode_body(payload)

        content = (
            f"Subject: {subject}\n"
            f"From: {sender}\n"
            f"Date: {date_str}\n"
            f"\n{body}"
        )

        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={
                "thread_id": data.get("threadId", ""),
                "label_ids": data.get("labelIds", []),
            },
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Gmail is a personal inbox; no shared permissions."""
        return []

    async def health_check(self) -> bool:
        """Verify connectivity by fetching the user profile."""
        if self._client is None:
            return False
        try:
            data = await self._get_json("/gmail/v1/users/me/profile")
            return "emailAddress" in data
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
