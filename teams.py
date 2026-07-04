"""Microsoft Teams connector.

API: Microsoft Graph API v1.0 (Teams channels + messages)
Auth: OAuth 2.0 client_credentials flow (tenant_id, client_id, client_secret)
Sync: Incremental (lastModifiedDateTime filter) + full
Permissions: Team membership -> PermissionEntry

Role mapping:
  Team owner   -> owner
  Team member  -> viewer
  Team guest   -> viewer
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

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

GRAPH_BASE = "https://graph.microsoft.com"
LOGIN_BASE = "https://login.microsoftonline.com"


class TeamsConnector(ConnectorBase):
    """Native Microsoft Teams connector via Graph API.

    Config:
        team_id: The Teams team ID to sync.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._team_id: str = config.get("team_id", "")
        self._client: httpx.AsyncClient | None = None
        self._access_token: str = ""

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate via OAuth 2.0 client_credentials flow.

        Expects credentials with: tenant_id, client_id, client_secret.
        """
        tenant_id = credentials.get("tenant_id", "")
        client_id = credentials.get("client_id", "")
        client_secret = credentials.get("client_secret", "")

        if not all([tenant_id, client_id, client_secret]):
            raise ConnectorAuthError(
                "Teams connector requires tenant_id, client_id, and client_secret",
                connector_type="teams",
            )

        token_url = f"{LOGIN_BASE}/{tenant_id}/oauth2/v2.0/token"

        async with httpx.AsyncClient(timeout=30.0) as token_client:
            try:
                resp = await token_client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise ConnectorAuthError(
                    f"Teams OAuth token request failed: {e.response.status_code}",
                    connector_type="teams",
                ) from e

        token_data = resp.json()
        self._access_token = token_data.get("access_token", "")
        if not self._access_token:
            raise ConnectorAuthError(
                "No access_token in OAuth response", connector_type="teams"
            )

        self._client = RetryClient(
            base_url=GRAPH_BASE,
            headers=bearer_headers(self._access_token),
            timeout=30.0,
            rate_limiter=self.rate_limiter,
        )
        logger.info("Teams connector authenticated for tenant %s", tenant_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET request via RetryClient (handles 429/5xx retries + rate limiting)."""
        assert self._client is not None
        try:
            return await self._client.get_json(url, params=params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ConnectorAuthError(
                    "Teams token expired or invalid", connector_type="teams"
                ) from e
            raise ConnectorTransientError(
                f"Teams API error: {e.response.status_code}", connector_type="teams"
            ) from e
        resp.raise_for_status()
        return resp.json()

    async def _list_channels(self) -> list[dict]:
        """Fetch all channels for the configured team."""
        url = f"/v1.0/teams/{self._team_id}/channels"
        channels: list[dict] = []
        while url:
            data = await self._get_json(url)
            channels.extend(data.get("value", []))
            url = data.get("@odata.nextLink", "")
        return channels

    # ------------------------------------------------------------------
    # ConnectorBase implementation
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Yield one DocumentMetadata per message across all channels.

        Uses delta query filtering on lastModifiedDateTime when ``since``
        is provided for incremental sync.
        """
        assert self._client is not None

        channels = await self._list_channels()

        for channel in channels:
            channel_id = channel["id"]
            channel_name = channel.get("displayName", channel_id)
            messages_url = (
                f"/v1.0/teams/{self._team_id}/channels/{channel_id}/messages"
            )
            # Use $filter for incremental sync — Graph API supports OData filtering
            if since:
                ts = since.strftime("%Y-%m-%dT%H:%M:%SZ")
                messages_url += f"?$filter=lastModifiedDateTime gt {ts}"

            next_url: str | None = messages_url
            while next_url:
                data = await self._get_json(next_url)
                for msg in data.get("value", []):
                    modified_str = msg.get("lastModifiedDateTime") or msg.get(
                        "createdDateTime", ""
                    )
                    if modified_str:
                        modified = datetime.fromisoformat(
                            modified_str.replace("Z", "+00:00")
                        )
                    else:
                        modified = datetime.now(UTC)

                    if since and modified < since:
                        continue

                    msg_id = msg.get("id", "")
                    subject = msg.get("subject") or ""
                    preview = (msg.get("body", {}).get("content") or "")[:120]

                    yield DocumentMetadata(
                        external_id=f"{channel_id}:{msg_id}",
                        title=subject or f"Message in #{channel_name}",
                        url=msg.get("webUrl"),
                        content_type="text/html",
                        author=msg.get("from", {})
                        .get("user", {})
                        .get("displayName"),
                        modified_at=modified,
                        folder_id=channel_id,
                        metadata={
                            "channel_name": channel_name,
                            "preview": preview,
                        },
                    )

                next_url = data.get("@odata.nextLink")

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single message and its replies.

        doc_id format: ``{channel_id}:{message_id}``
        """
        assert self._client is not None

        channel_id, message_id = doc_id.split(":", 1)
        msg_url = (
            f"/v1.0/teams/{self._team_id}/channels/{channel_id}"
            f"/messages/{message_id}"
        )
        msg = await self._get_json(msg_url)

        parts: list[str] = []
        author = msg.get("from", {}).get("user", {}).get("displayName", "unknown")
        body = msg.get("body", {}).get("content", "")
        created = msg.get("createdDateTime", "")
        parts.append(f"[{author} @ {created}]\n{body}")

        # Fetch replies
        replies_url = f"{msg_url}/replies"
        next_url: str | None = replies_url
        while next_url:
            data = await self._get_json(next_url)
            for reply in data.get("value", []):
                r_author = (
                    reply.get("from", {}).get("user", {}).get("displayName", "unknown")
                )
                r_body = reply.get("body", {}).get("content", "")
                r_time = reply.get("createdDateTime", "")
                parts.append(f"  [{r_author} @ {r_time}]\n  {r_body}")
            next_url = data.get("@odata.nextLink")

        content = "\n\n".join(parts)
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={"reply_count": len(parts) - 1},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Return team members as permission entries."""
        assert self._client is not None
        entries: list[PermissionEntry] = []

        url: str | None = f"/v1.0/teams/{self._team_id}/members"
        while url:
            data = await self._get_json(url)
            for member in data.get("value", []):
                roles = member.get("roles", [])
                relation = "owner" if "owner" in roles else "viewer"
                email = member.get("email", member.get("userId", ""))
                entries.append(
                    PermissionEntry(
                        subject_type="user",
                        subject_id=email,
                        relation=relation,
                    )
                )
            url = data.get("@odata.nextLink")

        return entries

    async def health_check(self) -> bool:
        """Verify connectivity by fetching team info."""
        if self._client is None:
            return False
        try:
            await self._get_json(f"/v1.0/teams/{self._team_id}")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
