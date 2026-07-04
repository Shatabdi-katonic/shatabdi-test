"""Discord connector.

API: Discord REST API v10 (guilds, channels, messages)
Auth: Bot token (Authorization: Bot {token})
Sync: Incremental (snowflake-based after parameter) + full
Permissions: Role-based channel permissions mapped to viewer entries

Permission mapping:
  Public channels (@everyone has VIEW_CHANNEL)  -> domain#viewer (guild-wide)
  Private channels (role/user overwrite grants)  -> user#viewer per member
  User-specific overwrites                       -> user#viewer / deny exclusion
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

DISCORD_API = "https://discord.com/api/v10"

# Discord epoch: 2015-01-01T00:00:00Z in milliseconds
DISCORD_EPOCH = 1_420_070_400_000

# Channel type 0 = GUILD_TEXT
TEXT_CHANNEL_TYPE = 0

# Discord permission bit for VIEW_CHANNEL (Read Messages)
VIEW_CHANNEL = 0x0000000000000400  # 1024


def _datetime_to_snowflake(dt: datetime) -> str:
    """Convert a datetime to a Discord snowflake ID for the ``after`` parameter."""
    ms = int(dt.timestamp() * 1000)
    snowflake = (ms - DISCORD_EPOCH) << 22
    return str(max(snowflake, 0))


def _parse_iso_timestamp(ts: str) -> datetime:
    """Parse a Discord ISO-8601 timestamp."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(UTC)


class DiscordConnector(ConnectorBase):
    """Native Discord connector via REST API.

    Config:
        server_id: The guild (server) ID to sync.
        channel_types: List of channel type ints to include. Default [0] (text).
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept canonical key (`server_id`) and frontend form key
        # (`discordServerId`). AddKnowledgeModal sends `discordServerId`
        # directly into config; without this fallback the connector hits
        # /guilds//channels with an empty server ID and silently returns
        # 0 documents (same bug pattern as the Outline connector).
        self._server_id: str = (
            config.get("server_id") or config.get("discordServerId") or ""
        )
        self._channel_types: list[int] = config.get(
            "channel_types", [TEXT_CHANNEL_TYPE]
        )
        # Capture optional channel allowlist from the frontend form
        # (`discordChannels` is a comma-separated string). When set,
        # _list_text_channels() filters to these IDs only. Empty set
        # means "sync all text channels" (preserves prior behavior).
        self._channel_filter: set[str] = self._parse_channel_filter(
            config.get("discordChannels")
        )
        # Hold onto a possible config-side bot token so authenticate()
        # can fall back when the sync route hasn't materialised one
        # into credentials. This mirrors the Outline connector fix.
        self._fallback_bot_token: str = config.get("discordBotToken") or ""
        self._client: httpx.AsyncClient | None = None
        self._guild_roles: dict[str, dict] | None = None

    @staticmethod
    def _parse_channel_filter(value: str | None) -> set[str]:
        """Parse comma-separated channel IDs into a set; empty -> no filter."""
        if not value or not isinstance(value, str):
            return set()
        return {c.strip() for c in value.split(",") if c.strip()}

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with a Discord bot token or OAuth access token.

        Expects credentials with: bot_token or access_token.
        For API-key sources created via AddKnowledgeModal the bot token
        arrives in config under `discordBotToken`; __init__ captures it
        as `_fallback_bot_token` and this method uses it when
        credentials don't carry one.
        """
        # Prefer the canonical credentials path. Fall back to the token
        # captured from config in __init__ — that's how API-key Discord
        # sources created via AddKnowledgeModal carry the bot token today.
        bot_token = credentials.get("bot_token") or self._fallback_bot_token or ""
        access_token = credentials.get("access_token", "")

        if bot_token:
            auth_header = f"Bot {bot_token}"
        elif access_token:
            auth_header = f"Bearer {access_token}"
        else:
            raise ConnectorAuthError(
                "Discord connector requires bot_token or access_token", connector_type="discord"
            )

        if not self._server_id:
            raise ConnectorAuthError(
                "Discord connector requires server_id in config",
                connector_type="discord",
            )

        self._client = RetryClient(
            base_url=DISCORD_API,
            headers={"Authorization": auth_header},
            timeout=30.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify the token by fetching the current bot user
        try:
            data = await self._get_json("/users/@me")
            logger.info(
                "Discord connector authenticated as %s#%s",
                data.get("username", "?"),
                data.get("discriminator", "0"),
            )
        except Exception as e:
            await self.close()
            raise ConnectorAuthError(
                f"Discord bot token verification failed: {e}",
                connector_type="discord",
            ) from e

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_json(self, url: str, params: dict | None = None) -> dict | list:
        """GET via RetryClient (handles 429/5xx retries + rate limiting)."""
        assert self._client is not None
        try:
            return await self._client.get_json(url, params=params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ConnectorAuthError(
                    "Discord bot token invalid or expired", connector_type="discord"
                ) from e
            raise ConnectorTransientError(
                f"Discord API error: {e.response.status_code}", connector_type="discord"
            ) from e

    async def _fetch_guild_roles(self) -> dict[str, dict]:
        """Fetch and cache guild roles keyed by role ID."""
        if self._guild_roles is not None:
            return self._guild_roles
        data = await self._get_json(f"/guilds/{self._server_id}/roles")
        if not isinstance(data, list):
            self._guild_roles = {}
            return self._guild_roles
        self._guild_roles = {role["id"]: role for role in data}
        return self._guild_roles

    async def _fetch_guild_members(self) -> AsyncIterator[dict]:
        """Paginate through all guild members."""
        last_id = "0"
        while True:
            params: dict = {"limit": "1000"}
            if last_id != "0":
                params["after"] = last_id
            data = await self._get_json(
                f"/guilds/{self._server_id}/members", params=params
            )
            if not isinstance(data, list) or len(data) == 0:
                break
            for member in data:
                yield member
            last_id = member["user"]["id"]
            if len(data) < 1000:
                break

    def _resolve_channel_viewers(
        self,
        overwrites: list[dict],
        roles: dict[str, dict],
    ) -> tuple[bool, set[str], set[str], set[str]]:
        """Determine who can view a channel based on permission overwrites.

        Returns:
            (everyone_can_view, allowed_role_ids, allowed_user_ids, denied_user_ids)
        """
        guild_id = self._server_id

        # Start with @everyone role base permissions
        everyone_role = roles.get(guild_id, {})
        base_perms = int(everyone_role.get("permissions", "0"))
        everyone_can_view = bool(base_perms & VIEW_CHANNEL)

        # Track per-role and per-user overrides
        allowed_role_ids: set[str] = set()
        denied_role_ids: set[str] = set()
        allowed_user_ids: set[str] = set()
        denied_user_ids: set[str] = set()

        for ow in overwrites:
            ow_id = ow["id"]
            ow_type = ow.get("type", 0)
            allow = int(ow.get("allow", "0"))
            deny = int(ow.get("deny", "0"))

            if ow_type == 0:  # role overwrite
                if ow_id == guild_id:
                    # @everyone role overwrite at channel level
                    if deny & VIEW_CHANNEL:
                        everyone_can_view = False
                    if allow & VIEW_CHANNEL:
                        everyone_can_view = True
                else:
                    if allow & VIEW_CHANNEL:
                        allowed_role_ids.add(ow_id)
                    if deny & VIEW_CHANNEL:
                        denied_role_ids.add(ow_id)
            elif ow_type == 1:  # member overwrite
                if allow & VIEW_CHANNEL:
                    allowed_user_ids.add(ow_id)
                if deny & VIEW_CHANNEL:
                    denied_user_ids.add(ow_id)

        # If @everyone can view and no @everyone deny, it's public
        # Roles explicitly denied are removed from the allowed set
        allowed_role_ids -= denied_role_ids

        return everyone_can_view, allowed_role_ids, allowed_user_ids, denied_user_ids

    async def _get_channel_permissions(
        self, channel_id: str
    ) -> list[PermissionEntry]:
        """Compute permission entries for a single channel."""
        channel_data = await self._get_json(f"/channels/{channel_id}")
        if not isinstance(channel_data, dict):
            return []

        overwrites = channel_data.get("permission_overwrites", [])
        roles = await self._fetch_guild_roles()

        everyone_can_view, allowed_role_ids, allowed_user_ids, denied_user_ids = (
            self._resolve_channel_viewers(overwrites, roles)
        )

        # Public channel: all guild members can view
        if everyone_can_view:
            return [
                PermissionEntry(
                    subject_type="domain",
                    subject_id=self._server_id,
                    relation="viewer",
                )
            ]

        # Private channel: enumerate members whose roles grant access
        entries: list[PermissionEntry] = []
        seen_user_ids: set[str] = set()

        # Add users with explicit user-level allow overwrites
        for user_id in allowed_user_ids:
            if user_id not in denied_user_ids:
                seen_user_ids.add(user_id)
                entries.append(
                    PermissionEntry(
                        subject_type="user",
                        subject_id=user_id,
                        relation="viewer",
                    )
                )

        # Paginate guild members and check role membership
        async for member in self._fetch_guild_members():
            user_id = member.get("user", {}).get("id", "")
            if not user_id or user_id in seen_user_ids or user_id in denied_user_ids:
                continue

            member_roles = set(member.get("roles", []))
            if member_roles & allowed_role_ids:
                seen_user_ids.add(user_id)
                entries.append(
                    PermissionEntry(
                        subject_type="user",
                        subject_id=user_id,
                        relation="viewer",
                    )
                )

        return entries

    async def _list_text_channels(self) -> list[dict]:
        """Fetch all text channels in the guild.

        When ``self._channel_filter`` is non-empty (driven by the
        `discordChannels` form field), only channels with matching IDs
        are returned. Empty filter means "all text channels" — preserves
        the previous default behavior.
        """
        data = await self._get_json(f"/guilds/{self._server_id}/channels")
        if not isinstance(data, list):
            return []
        text_channels = [
            ch for ch in data if ch.get("type", -1) in self._channel_types
        ]
        if self._channel_filter:
            text_channels = [
                ch for ch in text_channels if ch.get("id") in self._channel_filter
            ]
        return text_channels

    async def _fetch_messages(
        self, channel_id: str, after: str | None = None
    ) -> AsyncIterator[dict]:
        """Paginate through channel messages using the ``after`` parameter."""
        last_id = after or "0"

        while True:
            params: dict = {"limit": "100"}
            if last_id != "0":
                params["after"] = last_id

            data = await self._get_json(
                f"/channels/{channel_id}/messages", params=params
            )
            if not isinstance(data, list) or len(data) == 0:
                break

            # Discord returns newest first; reverse for chronological order
            data.sort(key=lambda m: m.get("id", "0"))

            for msg in data:
                yield msg

            last_id = data[-1]["id"]

            # If fewer than 100 messages returned, we've reached the end
            if len(data) < 100:
                break

    # ------------------------------------------------------------------
    # ConnectorBase implementation
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Yield one DocumentMetadata per message across all text channels.

        For incremental sync, converts ``since`` to a Discord snowflake and
        uses the ``after`` query parameter.
        """
        assert self._client is not None

        channels = await self._list_text_channels()
        after_snowflake = _datetime_to_snowflake(since) if since else None

        for channel in channels:
            channel_id = channel["id"]
            channel_name = channel.get("name", channel_id)

            async for msg in self._fetch_messages(channel_id, after=after_snowflake):
                ts_str = msg.get("timestamp", "")
                modified = _parse_iso_timestamp(ts_str)

                if since and modified < since:
                    continue

                author = msg.get("author", {})
                author_name = author.get("username", "unknown")
                content_preview = (msg.get("content") or "")[:120]

                yield DocumentMetadata(
                    external_id=f"{channel_id}:{msg['id']}",
                    title=f"Message by {author_name} in #{channel_name}",
                    url=f"https://discord.com/channels/{self._server_id}/{channel_id}/{msg['id']}",
                    content_type="text/plain",
                    author=author_name,
                    modified_at=modified,
                    folder_id=channel_id,
                    metadata={
                        "channel_name": channel_name,
                        "preview": content_preview,
                    },
                )

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single Discord message.

        doc_id format: ``{channel_id}:{message_id}``
        Returns the message formatted with author, timestamp, and content.
        """
        assert self._client is not None

        channel_id, message_id = doc_id.split(":", 1)
        data = await self._get_json(f"/channels/{channel_id}/messages/{message_id}")
        if not isinstance(data, dict):
            raise ConnectorTransientError(
                f"Unexpected response for message {doc_id}", connector_type="discord"
            )

        author = data.get("author", {})
        author_name = author.get("username", "unknown")
        timestamp = data.get("timestamp", "")
        body = data.get("content", "")

        # Include attachments info
        attachments = data.get("attachments", [])
        attachment_lines = [
            f"  Attachment: {a.get('filename', '?')} ({a.get('url', '')})"
            for a in attachments
        ]

        # Include embeds titles
        embeds = data.get("embeds", [])
        embed_lines = [
            f"  Embed: {e.get('title', '(untitled)')}" for e in embeds if e.get("title")
        ]

        parts = [f"[{author_name} @ {timestamp}]", body]
        if attachment_lines:
            parts.append("\n".join(attachment_lines))
        if embed_lines:
            parts.append("\n".join(embed_lines))

        content = "\n".join(parts)
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={
                "author_id": author.get("id", ""),
                "has_attachments": len(attachments) > 0,
            },
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Resolve Discord channel permissions for a message.

        doc_id format: ``{channel_id}:{message_id}``
        Extracts the channel_id and returns viewer entries based on role/user
        permission overwrites for VIEW_CHANNEL.
        """
        assert self._client is not None
        channel_id = doc_id.split(":", 1)[0]
        return await self._get_channel_permissions(channel_id)

    async def get_folder_permissions(self, folder_id: str) -> list[PermissionEntry]:
        """Resolve Discord channel permissions for a channel (folder).

        folder_id is a channel_id directly.
        """
        assert self._client is not None
        return await self._get_channel_permissions(folder_id)

    async def health_check(self) -> bool:
        """Verify connectivity by fetching guild info."""
        if self._client is None:
            return False
        try:
            data = await self._get_json(f"/guilds/{self._server_id}")
            return isinstance(data, dict) and "id" in data
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()