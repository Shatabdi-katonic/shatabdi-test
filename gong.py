"""Gong connector.

API: Gong REST API v2
Auth: Basic auth (access_key:secret_key)
Sync: Incremental (fromDateTime filter on calls) + full
Permissions: Not supported (Gong uses org-level access)

Content types indexed:
  - Call recordings (transcripts with speaker names and timestamps)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

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

GONG_BASE = "https://api.gong.io/v2"


class GongConnector(ConnectorBase):
    """Gong connector for call transcripts."""

    def __init__(self, config: dict | None = None) -> None:
        self._client: httpx.AsyncClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with Gong using Basic auth (access_key + secret_key)."""
        access_key = credentials.get("access_key", "")
        secret_key = credentials.get("secret_key", "")
        if not access_key or not secret_key:
            raise ConnectorAuthError(
                "Gong connector requires access_key and secret_key",
                connector_type="gong",
            )

        import base64 as _b64
        from platform_knowledge_engine.connectors._utils.http_client import RetryClient
        cred = _b64.b64encode(f"{access_key}:{secret_key}".encode()).decode()
        self._client = RetryClient(
            base_url=GONG_BASE,
            headers={"Authorization": f"Basic {cred}", "Content-Type": "application/json"},
            timeout=30.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify credentials
        resp = await self._request("GET", "/settings")
        if resp.status_code == 401:
            await self._client.aclose()
            self._client = None
            raise ConnectorAuthError("Invalid Gong credentials", connector_type="gong")
        logger.info("Gong authenticated successfully")

    # ------------------------------------------------------------------
    # list_documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List Gong calls, optionally filtered by date."""
        assert self._client is not None

        cursor: str | None = None
        while True:
            body: dict = {"filter": {}}
            if since:
                body["filter"]["fromDateTime"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            if cursor:
                body["cursor"] = cursor

            resp = await self._request("POST", "/calls/list", json=body)
            data = resp.json()

            for call in data.get("calls", []):
                call_id = call.get("id", "")
                started = call.get("started", "")
                title = call.get("title", "Untitled Call")
                duration = call.get("duration")
                parties = call.get("parties", [])

                organizer = ""
                for party in parties:
                    if party.get("affiliation") == "Internal":
                        organizer = party.get("emailAddress", "")
                        break

                yield DocumentMetadata(
                    external_id=f"gong:call:{call_id}",
                    title=title,
                    url=call.get("url"),
                    content_type="text/plain",
                    author=organizer or None,
                    modified_at=_parse_dt(started),
                    metadata={
                        "type": "call",
                        "duration_seconds": duration,
                        "source": "gong",
                    },
                )

            records_meta = data.get("records", {})
            cursor = records_meta.get("cursor")
            if not cursor or not records_meta.get("currentPageSize"):
                break

    # ------------------------------------------------------------------
    # fetch_document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a Gong call transcript by external ID."""
        assert self._client is not None
        parts = doc_id.split(":")
        if len(parts) < 3 or parts[0] != "gong":
            raise ValueError(f"Invalid Gong doc_id format: {doc_id}")

        call_id = parts[2]

        # Fetch transcript
        resp = await self._request(
            "POST",
            "/calls/transcript",
            json={"filter": {"callIds": [call_id]}},
        )
        data = resp.json()

        transcripts = data.get("callTranscripts", [])
        if not transcripts:
            return RawDocument(
                external_id=doc_id,
                content=b"(no transcript available)",
                content_type="text/plain",
                metadata={"type": "call_transcript"},
            )

        # Build human-readable transcript
        transcript = transcripts[0]
        lines: list[str] = [f"# Call Transcript: {call_id}", ""]

        for segment in transcript.get("transcript", []):
            speaker = segment.get("speakerName", segment.get("speakerId", "Unknown"))
            topic = segment.get("topic", "")

            if topic:
                lines.append(f"\n## {topic}\n")

            for sentence in segment.get("sentences", []):
                start = sentence.get("start", 0)
                text = sentence.get("text", "")
                timestamp = _format_millis(start)
                lines.append(f"[{timestamp}] **{speaker}:** {text}")

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=doc_id,
            content=content,
            content_type="text/plain",
            metadata={"filename": f"call-{call_id}.txt", "type": "call_transcript"},
        )

    # ------------------------------------------------------------------
    # get_permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Gong does not expose document-level permissions."""
        return []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            resp = await self._request("GET", "/settings")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # internal HTTP helper
    # ------------------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an HTTP request with rate-limit and error handling."""
        assert self._client is not None

        for attempt in range(4):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"Gong request timed out: {exc}", connector_type="gong"
                ) from exc

            if resp.status_code == 401:
                raise ConnectorAuthError("Gong auth failed (401)", connector_type="gong")

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "10"))
                if attempt < 3:
                    logger.warning("Gong rate limited, sleeping %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                raise ConnectorRateLimitError(
                    "Gong rate limit exceeded",
                    connector_type="gong",
                    retry_after=retry_after,
                )

            if resp.status_code >= 500:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"Gong server error {resp.status_code}", connector_type="gong"
                )

            resp.raise_for_status()
            return resp

        raise ConnectorTransientError("Gong max retries exceeded", connector_type="gong")


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime from Gong API."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _format_millis(ms: int) -> str:
    """Format milliseconds as HH:MM:SS."""
    total_seconds = ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
