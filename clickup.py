"""ClickUp connector.

API: ClickUp REST API v2
Auth: Bearer api_token (or plain Authorization header)
Sync: Incremental (date_updated_gt filter) with page-based pagination
Permissions: Not supported (returns empty)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

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

_API_BASE = "https://api.clickup.com"


class ClickUpConnector(ConnectorBase):
    """Native ClickUp connector for tasks.

    Config:
        team_id: ClickUp workspace (team) ID (required).
        space_id: Optional space ID to filter tasks.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._team_id: str = config.get("team_id", "")
        # Accept the frontend form keys (clickupSpace) alongside the canonical
        # key (space_id) — same field-mapping fallback pattern as the Outline
        # connector. Without this the user-entered Space ID is dropped.
        self._space_id: str | None = config.get("space_id") or config.get("clickupSpace")
        # Token entered in the AddKnowledgeModal API-token field arrives in
        # config as `clickupToken`; hold it so authenticate() can fall back to
        # it when credentials don't carry an access_token/api_token.
        self._config_token: str = config.get("clickupToken") or config.get("api_token") or ""
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using an API token (personal token or OAuth access token)."""
        # Accept every place the token can legitimately arrive:
        #   - credentials.access_token  → OAuth flow (auth_type=oauth)
        #   - credentials.api_token/api_key → api_key sources / inline config spread
        #   - config.clickupToken (captured as _config_token) → the modal's
        #     "API Token" form field, which previously never reached the connector
        #     ("ClickUp requires 'api_token' credential" on every sync).
        token = (
            credentials.get("access_token")
            or credentials.get("api_token")
            or credentials.get("api_key")
            or self._config_token
            or ""
        )
        if not token:
            raise ConnectorAuthError(
                "ClickUp requires an API token (OAuth access_token or a personal token)",
                connector_type="clickup",
            )

        headers = {"Authorization": token}
        self._client = RetryClient(base_url=_API_BASE, headers=headers, rate_limiter=self.rate_limiter)

        try:
            data = await self._client.get_json("/api/v2/user")
            user = data.get("user", {})
            logger.info("ClickUp authenticated as %s", user.get("username", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(
                f"ClickUp authentication failed: {exc}",
                connector_type="clickup",
            ) from exc

    async def _discover_team_id(self) -> str:
        """Return the authenticated user's first ClickUp team (workspace) id.

        The frontend collects no team/workspace id, so fall back to the
        token's first team. Returns "" on any failure (caller raises a clear
        error). GET /api/v2/team → {"teams": [{"id", "name", ...}]}.
        """
        assert self._client is not None
        try:
            data = await self._client.get_json("/api/v2/team")
            teams = data.get("teams", [])
            if teams:
                tid = str(teams[0].get("id", "") or "")
                logger.info(
                    "clickup_discover_team id=%s name=%s",
                    tid, teams[0].get("name", "?"),
                )
                return tid
        except Exception as exc:
            logger.warning("clickup_team_discovery_failed: %s", exc)
        return ""

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List tasks from the configured team, optionally filtered by date_updated_gt."""
        assert self._client is not None

        # The AddKnowledgeModal does not collect a team/workspace id, so
        # auto-discover the authenticated user's first team (workspace) when
        # none is configured — mirrors the Asana connector's workspace
        # auto-detect. Only fail if discovery also turns up nothing.
        if not self._team_id:
            self._team_id = await self._discover_team_id()
        if not self._team_id:
            raise ConnectorTransientError(
                "ClickUp requires 'team_id' in config (and none could be "
                "auto-discovered for this token)",
                connector_type="clickup",
            )

        page = 0
        while True:
            params: dict[str, str] = {
                "page": str(page),
                "include_closed": "true",
                "subtasks": "true",
            }

            if since:
                # ClickUp expects milliseconds since epoch
                ms = int(since.timestamp() * 1000)
                params["date_updated_gt"] = str(ms)

            if self._space_id:
                params["space_ids[]"] = self._space_id

            try:
                data = await self._client.get_json(
                    f"/api/v2/team/{self._team_id}/task",
                    params=params,
                )
            except Exception as exc:
                _raise_mapped(exc, "clickup")
                raise

            tasks = data.get("tasks", [])
            if not tasks:
                break

            for task in tasks:
                assignees = task.get("assignees", [])
                assignee_names = [a.get("username", "") for a in assignees]

                updated_ms = task.get("date_updated")
                modified_at = (
                    datetime.fromtimestamp(int(updated_ms) / 1000, tz=UTC)
                    if updated_ms
                    else datetime.now(UTC)
                )

                status = task.get("status", {})
                yield DocumentMetadata(
                    external_id=task["id"],
                    title=task.get("name", ""),
                    url=task.get("url"),
                    content_type="text/plain",
                    author=assignee_names[0] if assignee_names else None,
                    modified_at=modified_at,
                    folder_id=task.get("space", {}).get("id"),
                    metadata={
                        "status": status.get("status"),
                        "priority": (task.get("priority") or {}).get("priority"),
                        "assignees": assignee_names,
                        "list_name": task.get("list", {}).get("name"),
                    },
                )

            # ClickUp returns fewer than expected when no more pages
            if data.get("last_page", False):
                break
            page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single ClickUp task."""
        assert self._client is not None

        try:
            data = await self._client.get_json(
                f"/api/v2/task/{doc_id}",
                params={"include_subtasks": "true"},
            )
        except Exception as exc:
            _raise_mapped(exc, "clickup")
            raise

        parts: list[str] = []
        parts.append(f"# {data.get('name', '')}")
        parts.append("")

        status = data.get("status", {})
        parts.append(f"**Status:** {status.get('status', '')}")

        priority = data.get("priority") or {}
        if priority.get("priority"):
            parts.append(f"**Priority:** {priority['priority']}")

        assignees = data.get("assignees", [])
        if assignees:
            names = [a.get("username", a.get("email", "")) for a in assignees]
            parts.append(f"**Assignees:** {', '.join(names)}")

        tags = data.get("tags", [])
        if tags:
            tag_names = [t.get("name", "") for t in tags]
            parts.append(f"**Tags:** {', '.join(tag_names)}")

        if data.get("due_date"):
            try:
                due = datetime.fromtimestamp(int(data["due_date"]) / 1000, tz=UTC)
                parts.append(f"**Due:** {due.strftime('%Y-%m-%d')}")
            except (ValueError, TypeError):
                pass

        parts.append("")

        if data.get("description"):
            parts.append(data["description"])
            parts.append("")

        # Include subtasks summary
        subtasks = data.get("subtasks", [])
        if subtasks:
            parts.append("## Subtasks")
            for st in subtasks:
                st_status = st.get("status", {}).get("status", "")
                parts.append(f"- [{st_status}] {st.get('name', '')}")

        content = "\n".join(parts)
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={"title": data.get("name", "")},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """ClickUp does not expose task-level permissions via API."""
        return []

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/api/v2/user")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _raise_mapped(exc: Exception, connector_type: str) -> None:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            raise ConnectorAuthError(str(exc), connector_type=connector_type) from exc
        if code == 429:
            retry_after = float(exc.response.headers.get("Retry-After", "10"))
            raise ConnectorRateLimitError(
                str(exc), connector_type=connector_type, retry_after=retry_after
            ) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
