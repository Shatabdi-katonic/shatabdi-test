"""Fireflies.ai connector.

API: Fireflies GraphQL API
Auth: Bearer API key
Sync: Incremental (date filter on transcripts) + full
Permissions: Not supported (Fireflies uses org-level access)

Content types indexed:
  - Meeting transcripts with speaker names
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

FIREFLIES_GRAPHQL = "https://api.fireflies.ai/graphql"

LIST_TRANSCRIPTS_QUERY = """
query ListTranscripts {
    transcripts {
        id
        title
        date
        duration
        organizer_email
        participants
    }
}
"""

FETCH_TRANSCRIPT_QUERY = """
query FetchTranscript($id: String!) {
    transcript(id: $id) {
        id
        title
        date
        duration
        organizer_email
        participants
        sentences {
            speaker_name
            text
            start_time
            end_time
        }
        summary {
            overview
            action_items
            keywords
        }
    }
}
"""


class FirefliesConnector(ConnectorBase):
    """Fireflies.ai connector for meeting transcripts."""

    def __init__(self, config: dict | None = None) -> None:
        self._client: httpx.AsyncClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with a Fireflies API key or OAuth access token."""
        api_key = credentials.get("api_key", "") or credentials.get("access_token", "")
        if not api_key:
            raise ConnectorAuthError(
                "Fireflies connector requires api_key", connector_type="fireflies"
            )

        from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers
        self._client = RetryClient(
            headers={**bearer_headers(api_key), "Content-Type": "application/json"},
            timeout=30.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify token with a lightweight query
        resp = await self._graphql({"query": "{ user { email } }"})
        if "errors" in resp and any(
            "auth" in str(e.get("message", "")).lower() for e in resp["errors"]
        ):
            await self._client.close()
            self._client = None
            raise ConnectorAuthError("Invalid Fireflies API key", connector_type="fireflies")
        logger.info("Fireflies authenticated successfully")

    # ------------------------------------------------------------------
    # list_documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all Fireflies transcripts, optionally filtered by date."""
        assert self._client is not None

        data = await self._graphql({"query": LIST_TRANSCRIPTS_QUERY})
        transcripts = (data.get("data") or {}).get("transcripts") or []

        for t in transcripts:
            date_val = t.get("date")
            modified = _parse_fireflies_date(date_val)

            if since and modified < since:
                continue

            transcript_id = t.get("id", "")
            title = t.get("title", "Untitled Meeting")
            duration = t.get("duration")

            yield DocumentMetadata(
                external_id=f"fireflies:transcript:{transcript_id}",
                title=title,
                content_type="text/plain",
                author=t.get("organizer_email"),
                modified_at=modified,
                metadata={
                    "type": "meeting_transcript",
                    "duration_minutes": round(duration / 60, 1) if duration else None,
                    "participants": t.get("participants", []),
                    "source": "fireflies",
                },
            )

    # ------------------------------------------------------------------
    # fetch_document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a Fireflies transcript by external ID."""
        assert self._client is not None
        parts = doc_id.split(":")
        if len(parts) < 3 or parts[0] != "fireflies":
            raise ValueError(f"Invalid Fireflies doc_id format: {doc_id}")

        transcript_id = parts[2]

        data = await self._graphql({
            "query": FETCH_TRANSCRIPT_QUERY,
            "variables": {"id": transcript_id},
        })

        transcript = (data.get("data") or {}).get("transcript")
        if not transcript:
            return RawDocument(
                external_id=doc_id,
                content=b"(transcript not found)",
                content_type="text/plain",
                metadata={"type": "meeting_transcript"},
            )

        title = transcript.get("title", "Untitled Meeting")
        sentences = transcript.get("sentences") or []
        summary = transcript.get("summary") or {}

        lines: list[str] = [f"# {title}", ""]

        # Add summary if available
        overview = summary.get("overview")
        if overview:
            lines.append("## Summary")
            lines.append(overview)
            lines.append("")

        action_items = summary.get("action_items")
        if action_items:
            lines.append("## Action Items")
            if isinstance(action_items, list):
                for item in action_items:
                    lines.append(f"- {item}")
            else:
                lines.append(str(action_items))
            lines.append("")

        # Add transcript
        lines.append("## Transcript")
        lines.append("")

        for sentence in sentences:
            speaker = sentence.get("speaker_name", "Unknown")
            text = sentence.get("text", "")
            start = sentence.get("start_time")

            timestamp = ""
            if start is not None:
                timestamp = f"[{_format_seconds(start)}] "

            lines.append(f"{timestamp}**{speaker}:** {text}")

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=doc_id,
            content=content,
            content_type="text/plain",
            metadata={"filename": f"transcript-{transcript_id}.txt", "type": "meeting_transcript"},
        )

    # ------------------------------------------------------------------
    # get_permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Fireflies does not expose document-level permissions."""
        return []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            data = await self._graphql({"query": "{ user { email } }"})
            return "errors" not in data
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    async def _graphql(self, payload: dict) -> dict:
        """Execute a GraphQL request against the Fireflies API."""
        assert self._client is not None

        for attempt in range(4):
            try:
                resp = await self._client.post(FIREFLIES_GRAPHQL, json=payload)
            except httpx.TimeoutException as exc:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"Fireflies request timed out: {exc}", connector_type="fireflies"
                ) from exc

            if resp.status_code == 401:
                raise ConnectorAuthError(
                    "Fireflies auth failed (401)", connector_type="fireflies"
                )

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "10"))
                if attempt < 3:
                    logger.warning("Fireflies rate limited, sleeping %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                raise ConnectorRateLimitError(
                    "Fireflies rate limit exceeded",
                    connector_type="fireflies",
                    retry_after=retry_after,
                )

            if resp.status_code >= 500:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"Fireflies server error {resp.status_code}", connector_type="fireflies"
                )

            resp.raise_for_status()
            return resp.json()

        raise ConnectorTransientError(
            "Fireflies max retries exceeded", connector_type="fireflies"
        )


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _parse_fireflies_date(val) -> datetime:
    """Parse date from Fireflies API (can be epoch ms or ISO string)."""
    if not val:
        return datetime.now(UTC)
    if isinstance(val, (int, float)):
        # Epoch milliseconds
        return datetime.fromtimestamp(val / 1000, tz=UTC)
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _format_seconds(seconds) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "00:00"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
