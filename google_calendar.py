"""Google Calendar connector.

API: Google Calendar API v3
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (updatedMin filter)
Permissions: Not supported
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers
from platform_knowledge_engine.connectors.base import (
    ConnectorAuthError, ConnectorBase, ConnectorRateLimitError,
    ConnectorTransientError, DocumentMetadata, PermissionEntry, RawDocument,
)

logger = logging.getLogger(__name__)
_BASE = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarConnector(ConnectorBase):
    """Native Google Calendar connector via Calendar API v3."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._calendar_ids: list[str] = config.get("calendar_ids", ["primary"])
        self._client: RetryClient | None = None
        # Owner identifier captured at authenticate-time. Used by
        # ``get_permissions`` to write a SpiceDB ``owner`` relation for every
        # ingested calendar event. Without this, the syncer wrote zero
        # relationships and the retriever permission filter excluded every
        # Google Calendar chunk from search results — same root cause as
        # the Miro phantom-chunks bug. The Calendar API exposes event-level
        # ACL via event.attendees, but those are external email addresses
        # that may not map to platform users; the static owner entry is
        # the safe baseline matching the miro.py / asana.py / airtable.py
        # / bamboohr.py / pipedrive.py pattern.
        self._owner_user_id: str = ""

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Google Calendar requires 'access_token'", connector_type="google_calendar")
        # Prefer the canonical platform user_id (Keycloak sub) injected by
        # the OAuth callback as ``platform_user_id``. Falls back to the
        # provider-native ``user_id`` for pre-fix records. See miro.py
        # for the full bug history.
        self._owner_user_id = (
            credentials.get("platform_user_id")
            or credentials.get("user_id")
            or ""
        ).strip()
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/users/me/calendarList", params={"maxResults": 1})
            resp.json()
            logger.info(
                "Google Calendar authenticated (owner=%s)",
                self._owner_user_id or "?",
            )
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Google Calendar auth failed: {exc}", connector_type="google_calendar") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        for cal_id in self._calendar_ids:
            page_token: str | None = None
            while True:
                params: dict = {"maxResults": 250, "singleEvents": "true", "orderBy": "updated"}
                if since:
                    params["updatedMin"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    params["timeMin"] = "2020-01-01T00:00:00Z"
                if page_token:
                    params["pageToken"] = page_token
                try:
                    resp = await self._client.get(f"/calendars/{cal_id}/events", params=params)
                except Exception as exc:
                    _raise_mapped(exc, "google_calendar")
                    raise
                body = resp.json()
                for event in body.get("items", []):
                    if event.get("status") == "cancelled":
                        continue
                    yield DocumentMetadata(
                        external_id=event["id"],
                        title=event.get("summary", "Untitled Event"),
                        url=event.get("htmlLink"),
                        content_type="text/plain",
                        author=((event.get("organizer") or {}).get("email")),
                        modified_at=_parse_ts(event.get("updated", "")),
                        folder_id=cal_id,
                        metadata={"calendar": cal_id, "status": event.get("status")},
                    )
                page_token = body.get("nextPageToken")
                if not page_token:
                    break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        cal_id = self._calendar_ids[0] if self._calendar_ids else "primary"
        try:
            resp = await self._client.get(f"/calendars/{cal_id}/events/{doc_id}")
        except Exception as exc:
            _raise_mapped(exc, "google_calendar")
            raise
        event = resp.json()
        parts = [f"# {event.get('summary', doc_id)}", ""]
        start = event.get("start", {})
        end = event.get("end", {})
        parts.append(f"**Start:** {start.get('dateTime', start.get('date', ''))}")
        parts.append(f"**End:** {end.get('dateTime', end.get('date', ''))}")
        if event.get("location"):
            parts.append(f"**Location:** {event['location']}")
        organizer = (event.get("organizer") or {}).get("email", "")
        if organizer:
            parts.append(f"**Organizer:** {organizer}")
        attendees = event.get("attendees", [])
        if attendees:
            names = [a.get("email", "") for a in attendees[:20]]
            parts.append(f"**Attendees:** {', '.join(names)}")
        parts.append("")
        if event.get("description"):
            parts.append(event["description"])
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": event.get("summary", "")})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Google Calendar API does not expose per-event ACLs in a form
        we can map to SpiceDB subjects (attendees are email addresses
        for external invitees, not platform users).

        Treat every ingested event as owned by the user who registered
        the credential — same pattern as miro.py, airtable.py, asana.py,
        bamboohr.py, pipedrive.py, and file_upload.py:162-172. Without
        this, the syncer wrote zero SpiceDB relationships and the
        retriever permission filter (retriever.py:626) silently dropped
        every Google Calendar chunk from search results.
        """
        if self._owner_user_id:
            return [
                PermissionEntry(
                    subject_type="user",
                    subject_id=self._owner_user_id,
                    relation="owner",
                )
            ]
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/users/me/calendarList", params={"maxResults": 1})
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _parse_ts(ts: str) -> datetime:
    if not ts:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _raise_mapped(exc: Exception, connector_type: str) -> None:
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            raise ConnectorAuthError(str(exc), connector_type=connector_type) from exc
        if code == 429:
            raise ConnectorRateLimitError(str(exc), connector_type=connector_type, retry_after=float(exc.response.headers.get("Retry-After", "5"))) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
