"""Zulip connector.

API: Zulip REST API v1 (streams, messages, users)
Auth: Basic auth (email + api_key)
Sync: Incremental (timestamp filter on messages) + full
Permissions: Stream membership -> PermissionEntry (viewer role)
"""

from __future__ import annotations

import json
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


class ZulipConnector(ConnectorBase):
    """Native Zulip connector via REST API.

    Config:
        server_url: The Zulip server URL (e.g. https://chat.example.com).
        include_private: Whether to include private streams. Default False.
        max_messages_per_stream: Max messages to fetch per stream. Default 100.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept the Add-Knowledge wizard's form key (zulipUrl) alongside the
        # canonical key (server_url) — the wizard sends zulipUrl/zulipEmail/
        # zulipApiKey, which the connector otherwise never reads, so a
        # UI-created source failed discovery with "requires server_url" and
        # stayed Pending / 0 docs. Same field-mapping fallback as Linear/Outline.
        self._server_url: str = (config.get("server_url") or config.get("zulipUrl") or "").rstrip("/")
        self._include_private: bool = config.get("include_private", False)
        self._max_messages: int = config.get("max_messages_per_stream", 100)
        self._client: httpx.AsyncClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with Zulip using email+API key (Basic) or OAuth access token."""
        from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers

        email = credentials.get("email") or credentials.get("zulipEmail") or ""
        api_key = credentials.get("api_key") or credentials.get("zulipApiKey") or ""
        access_token = credentials.get("access_token", "")

        _cred_server = credentials.get("server_url") or credentials.get("zulipUrl")
        if _cred_server:
            self._server_url = _cred_server.rstrip("/")

        if not self._server_url:
            raise ConnectorAuthError(
                "Zulip connector requires server_url in config or credentials",
                connector_type="zulip",
            )

        base_url = f"{self._server_url}/api/v1"

        if access_token:
            # OAuth Bearer token
            self._client = RetryClient(
                base_url=base_url,
                headers=bearer_headers(access_token),
                timeout=30.0,
                rate_limiter=self.rate_limiter,
            )
        elif email and api_key:
            # Basic auth (email + API key)
            import base64 as _b64
            cred = _b64.b64encode(f"{email}:{api_key}".encode()).decode()
            self._client = RetryClient(
                base_url=base_url,
                headers={"Authorization": f"Basic {cred}"},
                timeout=30.0,
                rate_limiter=self.rate_limiter,
            )
        else:
            raise ConnectorAuthError(
                "Zulip connector requires access_token (OAuth) or email + api_key",
                connector_type="zulip",
            )

        # Verify credentials
        try:
            data = await self._get_json("/users/me")
            if data.get("result") != "success":
                raise ConnectorAuthError(
                    f"Zulip auth failed: {data.get('msg', 'unknown error')}",
                    connector_type="zulip",
                )
            logger.info(
                "Zulip connector authenticated as %s",
                data.get("full_name", email),
            )
        except ConnectorAuthError:
            await self.close()
            raise
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"Zulip auth verification failed: {e}", connector_type="zulip"
            ) from e

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET with standard error handling for Zulip API."""
        assert self._client is not None
        try:
            resp = await self._client.get(url, params=params)
        except httpx.TimeoutException as e:
            raise ConnectorTransientError(
                f"Zulip API timeout: {e}", connector_type="zulip"
            ) from e

        if resp.status_code == 401:
            raise ConnectorAuthError(
                "Zulip credentials invalid", connector_type="zulip"
            )
        if resp.status_code == 429:
            retry_data = resp.json()
            retry_after = float(retry_data.get("retry-after", 5))
            raise ConnectorRateLimitError(
                "Zulip API rate limited",
                connector_type="zulip",
                retry_after=retry_after,
            )
        if resp.status_code >= 500:
            raise ConnectorTransientError(
                f"Zulip API server error: {resp.status_code}",
                connector_type="zulip",
            )
        resp.raise_for_status()
        return resp.json()

    async def _list_streams(self) -> list[dict]:
        """Fetch all accessible streams."""
        data = await self._get_json("/streams")
        if data.get("result") != "success":
            logger.error("Failed to list streams: %s", data.get("msg"))
            return []

        streams = data.get("streams", [])
        if not self._include_private:
            streams = [s for s in streams if not s.get("invite_only", False)]

        return streams

    async def _fetch_stream_messages(
        self, stream_name: str, since: datetime | None = None
    ) -> AsyncIterator[dict]:
        """Fetch recent messages from a stream with optional timestamp filter."""
        narrow = json.dumps([{"operator": "stream", "operand": stream_name}])
        params: dict = {
            "anchor": "newest",
            "num_before": str(self._max_messages),
            "num_after": "0",
            "narrow": narrow,
        }

        data = await self._get_json("/messages", params=params)
        if data.get("result") != "success":
            logger.warning(
                "Failed to fetch messages for stream %s: %s",
                stream_name,
                data.get("msg"),
            )
            return

        messages = data.get("messages", [])
        since_ts = since.timestamp() if since else 0

        for msg in messages:
            msg_ts = msg.get("timestamp", 0)
            if since_ts and msg_ts <= since_ts:
                continue
            yield msg

    # ------------------------------------------------------------------
    # ConnectorBase implementation
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Yield one DocumentMetadata per message across all streams."""
        assert self._client is not None

        streams = await self._list_streams()

        for stream in streams:
            stream_name = stream.get("name", "")
            stream_id = stream.get("stream_id", 0)

            async for msg in self._fetch_stream_messages(stream_name, since=since):
                msg_id = str(msg.get("id", ""))
                timestamp = msg.get("timestamp", 0)
                modified = datetime.fromtimestamp(timestamp, tz=UTC)
                sender = msg.get("sender_full_name", "unknown")
                topic = msg.get("subject", "")  # Zulip calls topics "subject"
                content_preview = (msg.get("content") or "")[:120]

                yield DocumentMetadata(
                    external_id=f"{stream_id}:{msg_id}",
                    title=f"{topic} - {sender}" if topic else f"Message by {sender}",
                    url=f"{self._server_url}/#narrow/stream/{stream_id}-{stream_name}/topic/{topic}/near/{msg_id}",
                    content_type="text/plain",
                    author=sender,
                    modified_at=modified,
                    folder_id=str(stream_id),
                    metadata={
                        "stream_name": stream_name,
                        "topic": topic,
                        "preview": content_preview,
                    },
                )

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single Zulip message by ID.

        doc_id format: ``{stream_id}:{message_id}``
        Returns the message formatted with sender, timestamp, topic, and content.
        """
        assert self._client is not None

        _stream_id, message_id = doc_id.split(":", 1)
        data = await self._get_json(f"/messages/{message_id}")

        if data.get("result") != "success":
            raise ConnectorTransientError(
                f"Failed to fetch message {doc_id}: {data.get('msg')}",
                connector_type="zulip",
            )

        msg = data.get("message", data)
        sender = msg.get("sender_full_name", "unknown")
        timestamp = msg.get("timestamp", 0)
        dt = datetime.fromtimestamp(timestamp, tz=UTC)
        topic = msg.get("subject", "")
        body = msg.get("content", "")
        stream = msg.get("display_recipient", "")

        content = (
            f"Stream: {stream}\n"
            f"Topic: {topic}\n"
            f"Sender: {sender}\n"
            f"Date: {dt.isoformat()}\n"
            f"\n{body}"
        )

        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={
                "topic": topic,
                "sender_email": msg.get("sender_email", ""),
                "stream": stream,
            },
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Return stream members as permission entries (viewer role)."""
        assert self._client is not None

        stream_id = doc_id.split(":", 1)[0]
        entries: list[PermissionEntry] = []

        try:
            data = await self._get_json(f"/streams/{stream_id}/members")
        except Exception:
            logger.warning("Failed to fetch members for stream %s", stream_id)
            return entries

        if data.get("result") != "success":
            return entries

        # Zulip returns subscriber user IDs; resolve to emails if possible
        subscribers = data.get("subscribers", [])
        for user_id in subscribers:
            entries.append(
                PermissionEntry(
                    subject_type="user",
                    subject_id=str(user_id),
                    relation="viewer",
                )
            )

        return entries

    async def health_check(self) -> bool:
        """Verify connectivity by fetching the server settings."""
        if self._client is None:
            return False
        try:
            data = await self._get_json("/server_settings")
            return data.get("result") == "success"
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()
