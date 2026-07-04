"""Slack connector.

API: Slack Web API (conversations.list, conversations.history, conversations.members)
Auth: OAuth 2.0 (Bot token with channels:history, channels:read scopes)
Sync: Incremental (oldest timestamp) + full
Permissions: Channel membership -> folder-level permissions

Role mapping (spec section 15.4):
  Public channel members  -> folder#viewer (all workspace members)
  Private channel members -> folder#viewer (only members)
  DM participants         -> document-level viewer
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import (
    RetryClient,
    bearer_headers,
)
from platform_knowledge_engine.connectors.base import (
    ConfigField,
    ConnectorAuthError,
    ConnectorBase,
    ConnectorTransientError,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

# Message subtypes to skip (bot messages, joins, etc.)
SKIP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "bot_add",
    "bot_remove",
    "ekm_access_denied",
}


class SlackConnector(ConnectorBase):
    """Native Slack connector via Web API.

    Config:
        channel_types: Types to sync. Default ["public_channel", "private_channel"].
        include_threads: Whether to fetch full thread replies. Default True.
        min_messages: Min messages in a conversation to create a document. Default 1.
    """

    CONFIG_SCHEMA = [
        ConfigField(
            key="slackChannels",
            label="Channel filter",
            type="text",
            required=False,
            placeholder="e.g. general, random, engineering",
            help_text="Comma-separated channel names to sync. Leave blank to sync all channels the bot can see.",
        ),
        ConfigField(
            key="include_threads",
            label="Include thread replies",
            type="boolean",
            required=False,
            default=True,
            help_text="When enabled, pulls full thread replies alongside parent messages.",
        ),
        ConfigField(
            key="min_messages",
            label="Minimum messages per channel",
            type="number",
            required=False,
            default=1,
            help_text="Skip channels with fewer messages than this threshold.",
        ),
    ]

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._channel_types: list[str] = config.get(
            "channel_types", ["public_channel", "private_channel"]
        )
        self._include_threads: bool = config.get("include_threads", True)
        self._min_messages: int = config.get("min_messages", 1)
        # Frontend field: slackBotToken as fallback for access_token
        self._bot_token: str = config.get("slackBotToken", "")
        # Frontend field: slackChannels as channel filter
        self._channel_filter: list[str] = []
        raw_channels = config.get("slackChannels", "")
        if raw_channels:
            self._channel_filter = [
                ch.strip() for ch in raw_channels.split(",") if ch.strip()
            ]
        self._client: RetryClient | None = None
        self._workspace_domain: str = ""

    async def authenticate(self, credentials: dict) -> None:
        token = (
            credentials.get("access_token", "")
            or credentials.get("slackBotToken", "")
            or self._bot_token
        )
        if not token:
            raise ConnectorAuthError(
                "Slack connector requires access_token or slackBotToken"
            )
        self._client = RetryClient(
            base_url=SLACK_API,
            headers=bearer_headers(token),
        )
        # Verify token and get workspace info
        data = await self._client.get_json("/auth.test")
        if not data.get("ok"):
            raise ConnectorAuthError(
                f"Slack auth failed: {data.get('error', 'unknown')}"
            )
        self._workspace_domain = data.get("url", "")
        logger.info("Slack authenticated to workspace: %s", data.get("team", ""))

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Each Slack channel becomes a document (messages concatenated)."""
        assert self._client is not None

        cursor: str | None = None
        while True:
            params: dict = {
                "types": ",".join(self._channel_types),
                "limit": "200",
                "exclude_archived": "false",
            }
            if cursor:
                params["cursor"] = cursor

            data = await self._client.get_json("/conversations.list", params=params)
            if not data.get("ok"):
                err = data.get("error", "unknown")
                logger.error("conversations.list failed: %s", err)
                raise ConnectorTransientError(
                    f"Slack conversations.list failed: {err}"
                )

            for channel in data.get("channels", []):
                # Use channel's latest message timestamp for modified_at.
                # NOTE: Slack returns `created` in epoch SECONDS but `updated` in
                # epoch MILLISECONDS. Feeding the ms value straight into
                # fromtimestamp() (as seconds) produces a year far beyond
                # datetime's max → "year 58399 is out of range" crash that fails
                # the whole discovery. Normalize: treat anything above ~1e11 as
                # milliseconds, and fall back to now() on any bad/zero value. (CR-568)
                channel_ts = float(
                    channel.get("updated", 0) or channel.get("created", 0) or 0
                )
                if channel_ts > 1e11:
                    channel_ts /= 1000.0
                try:
                    modified = (
                        datetime.fromtimestamp(channel_ts, tz=UTC)
                        if channel_ts > 0
                        else datetime.now(tz=UTC)
                    )
                except (ValueError, OverflowError, OSError):
                    modified = datetime.now(tz=UTC)

                if since and modified < since:
                    continue

                yield DocumentMetadata(
                    external_id=channel["id"],
                    title=f"#{channel.get('name', channel['id'])}",
                    url=f"{self._workspace_domain}archives/{channel['id']}",
                    content_type="text/plain",
                    modified_at=modified,
                    folder_id=channel["id"],
                    metadata={
                        "is_private": channel.get("is_private", False),
                        "num_members": channel.get("num_members", 0),
                        "topic": channel.get("topic", {}).get("value", ""),
                        "purpose": channel.get("purpose", {}).get("value", ""),
                    },
                )

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch all messages from a channel and concatenate as text."""
        assert self._client is not None
        messages: list[str] = []
        cursor: str | None = None

        while True:
            params: dict = {"channel": doc_id, "limit": "200"}
            if cursor:
                params["cursor"] = cursor

            data = await self._client.get_json("/conversations.history", params=params)
            if not data.get("ok"):
                logger.warning(
                    "Slack conversations.history failed for channel %s: %s",
                    doc_id, data.get("error", "unknown"),
                )
                break

            for msg in data.get("messages", []):
                subtype = msg.get("subtype", "")
                if subtype in SKIP_SUBTYPES:
                    continue

                user = msg.get("user", "unknown")
                text = msg.get("text", "")
                ts = msg.get("ts", "")

                if text.strip():
                    formatted = f"[{user}] {text}"
                    messages.append(formatted)

                    # Fetch thread replies if enabled
                    if self._include_threads and int(msg.get("reply_count", 0)) > 0:
                        thread_msgs = await self._fetch_thread(doc_id, ts)
                        messages.extend(thread_msgs)

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        content = "\n\n".join(reversed(messages))  # Chronological order
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={"message_count": len(messages)},
        )

    async def _fetch_thread(self, channel_id: str, thread_ts: str) -> list[str]:
        """Fetch replies in a thread."""
        assert self._client is not None
        messages: list[str] = []

        data = await self._client.get_json(
            "/conversations.replies",
            params={"channel": channel_id, "ts": thread_ts, "limit": "100"},
        )

        if not data.get("ok"):
            logger.warning("Slack thread fetch failed for %s:%s: %s", channel_id, thread_ts, data.get("error", "unknown"))
        if data.get("ok"):
            for msg in data.get("messages", [])[1:]:  # Skip parent
                user = msg.get("user", "unknown")
                text = msg.get("text", "")
                if text.strip():
                    messages.append(f"  [{user}] {text}")

        return messages

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get channel permissions.

        Public channels: returns a domain-level entry granting access to
        all workspace members (since anyone in the workspace can join).
        Private channels: enumerates individual members.
        """
        assert self._client is not None
        entries: list[PermissionEntry] = []

        # Check if channel is public — all workspace members have implicit access
        try:
            info = await self._client.get_json("/conversations.info", params={"channel": doc_id})
            if info.get("ok"):
                channel = info.get("channel", {})
                if not channel.get("is_private", True):
                    # Public channel — grant workspace-wide access
                    workspace_domain = self._workspace_domain or "workspace"
                    entries.append(PermissionEntry(
                        subject_type="domain",
                        subject_id=workspace_domain,
                        relation="viewer",
                    ))
                    return entries
        except Exception:
            pass  # Fall through to member enumeration

        cursor: str | None = None

        while True:
            params: dict = {"channel": doc_id, "limit": "200"}
            if cursor:
                params["cursor"] = cursor

            data = await self._client.get_json("/conversations.members", params=params)
            if not data.get("ok"):
                break

            for member_id in data.get("members", []):
                entries.append(
                    PermissionEntry(
                        subject_type="user",
                        subject_id=member_id,
                        relation="viewer",
                    )
                )

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return entries

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            data = await self._client.get_json("/auth.test")
            return data.get("ok", False)
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()
