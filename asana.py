"""Asana connector.

API: Asana REST API v1.0
Auth: Bearer personal_access_token
Sync: Incremental (modified_since filter) with offset pagination
Permissions: Not supported (returns empty)
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
    ConnectorRateLimitError,
    ConnectorTransientError,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)

_API_BASE = "https://app.asana.com"


class AsanaConnector(ConnectorBase):
    """Native Asana connector for tasks.

    Config:
        project_gid: Asana project GID to sync. Mutually exclusive with workspace_gid.
        workspace_gid: Asana workspace GID. Used if project_gid is not set.
    """

    CONFIG_SCHEMA = [
        ConfigField(
            key="project_gid",
            label="Project GID",
            type="text",
            required=False,
            placeholder="e.g. 1203456789012345",
            help_text="Specific Asana project to sync. Leave blank to sync across the workspace.",
        ),
        ConfigField(
            key="workspace_gid",
            label="Workspace GID",
            type="text",
            required=False,
            placeholder="e.g. 1203456789012340",
            help_text="Workspace to sync when no project is given. Leave blank to auto-detect the user's first workspace.",
        ),
    ]

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._project_gid: str | None = config.get("project_gid")
        self._workspace_gid: str | None = config.get("workspace_gid")
        self._client: RetryClient | None = None
        # Owner identifier captured at authenticate-time. Used by
        # ``get_permissions`` to write a SpiceDB ``owner`` relation for every
        # ingested Asana task. Without this, the syncer wrote zero
        # relationships and the retriever permission filter excluded every
        # Asana chunk from search results — same root cause as the Miro bug.
        # Prefers platform user_id from credentials; falls back to the Asana
        # ``/users/me`` GID captured during authenticate.
        self._owner_user_id: str = ""

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using a personal access token."""
        token = credentials.get("personal_access_token") or credentials.get("api_key") or credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError(
                "Asana requires 'personal_access_token' credential",
                connector_type="asana",
            )

        # First preference: the canonical platform user_id (Keycloak sub)
        # injected by the OAuth callback as ``platform_user_id``. Falls back
        # to provider-native ``user_id`` only when missing. See miro.py for
        # the full bug history — Asana's OAuth response carries a numeric
        # ``user_id`` that doesn't match Keycloak UUID format, so the
        # IdentityResolver drops the entry unless we use the canonical
        # platform_user_id instead.
        self._owner_user_id = (
            credentials.get("platform_user_id")
            or credentials.get("user_id")
            or ""
        ).strip()

        headers = bearer_headers(token)
        self._client = RetryClient(base_url=_API_BASE, headers=headers, rate_limiter=self.rate_limiter)

        try:
            data = await self._client.get_json("/api/1.0/users/me")
            user = data.get("data", {})
            logger.info("Asana authenticated as %s", user.get("name", "?"))
            # Fallback to the Asana native GID when credentials didn't carry
            # the platform user_id. IdentityResolver maps it to the canonical
            # platform user at search-time via the credential-store mapping.
            if not self._owner_user_id:
                self._owner_user_id = str(user.get("gid") or "").strip()
            # Auto-detect workspace when neither project_gid nor workspace_gid is
            # configured. /users/me returns the user's workspace memberships —
            # use the first one so an unconfigured source still yields tasks
            # instead of silently returning zero.
            if not self._project_gid and not self._workspace_gid:
                workspaces = user.get("workspaces") or []
                if workspaces:
                    self._workspace_gid = workspaces[0].get("gid")
                    logger.info(
                        "asana_discover_workspace gid=%s name=%s",
                        self._workspace_gid,
                        workspaces[0].get("name", "?"),
                    )
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(
                f"Asana authentication failed: {exc}",
                connector_type="asana",
            ) from exc

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List tasks from a project or workspace with optional modified_since filter."""
        assert self._client is not None

        params: dict[str, str] = {
            "limit": "100",
            "opt_fields": "name,modified_at,assignee.name,completed,permalink_url",
        }

        if self._project_gid:
            params["project"] = self._project_gid
        elif self._workspace_gid:
            params["workspace"] = self._workspace_gid
            # GET /api/1.0/tasks requires an `assignee` when filtering by
            # `workspace`. The param is `assignee` (a user gid or "me") — NOT
            # `assignee.any`, which belongs to the SEARCH endpoint
            # (/workspaces/{gid}/tasks/search). Sending `assignee.any` here made
            # Asana reject the request with HTTP 400 → discovery failed → a
            # 0-document sync stuck in "Pending"/error.
            params["assignee"] = "me"
        else:
            raise ConnectorTransientError(
                "Asana requires either 'project_gid' or 'workspace_gid' in config",
                connector_type="asana",
            )

        if since:
            params["modified_since"] = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        offset: str | None = None

        while True:
            if offset:
                params["offset"] = offset

            try:
                resp = await self._client.get_json("/api/1.0/tasks", params=params)
            except Exception as exc:
                _raise_mapped(exc, "asana")
                raise

            tasks = resp.get("data", [])
            for task in tasks:
                assignee = task.get("assignee") or {}
                yield DocumentMetadata(
                    external_id=task["gid"],
                    title=task.get("name", ""),
                    url=task.get("permalink_url"),
                    content_type="text/plain",
                    author=assignee.get("name"),
                    modified_at=_parse_ts(task.get("modified_at", "")),
                    metadata={
                        "completed": task.get("completed", False),
                        "assignee": assignee.get("name"),
                    },
                )

            next_page = resp.get("next_page")
            if next_page and next_page.get("offset"):
                offset = next_page["offset"]
            else:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single Asana task with full details."""
        assert self._client is not None

        opt_fields = "name,notes,completed,assignee.name,custom_fields,permalink_url,due_on,tags.name"
        try:
            resp = await self._client.get_json(
                f"/api/1.0/tasks/{doc_id}",
                params={"opt_fields": opt_fields},
            )
        except Exception as exc:
            _raise_mapped(exc, "asana")
            raise

        task = resp.get("data", {})
        parts: list[str] = []

        parts.append(f"# {task.get('name', '')}")
        parts.append("")

        assignee = (task.get("assignee") or {}).get("name", "Unassigned")
        parts.append(f"**Assignee:** {assignee}")
        parts.append(f"**Completed:** {task.get('completed', False)}")
        if task.get("due_on"):
            parts.append(f"**Due:** {task['due_on']}")

        tags = task.get("tags", [])
        if tags:
            tag_names = [t.get("name", "") for t in tags]
            parts.append(f"**Tags:** {', '.join(tag_names)}")
        parts.append("")

        if task.get("notes"):
            parts.append(task["notes"])
            parts.append("")

        # Custom fields
        custom_fields = task.get("custom_fields", [])
        if custom_fields:
            parts.append("## Custom Fields")
            for cf in custom_fields:
                name = cf.get("name", "")
                display = cf.get("display_value") or cf.get("text_value") or str(cf.get("number_value", ""))
                if name and display:
                    parts.append(f"- **{name}:** {display}")

        content = "\n".join(parts)
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={"title": task.get("name", "")},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Asana does not expose task-level permissions via REST.

        Treat every ingested task as owned by the user who registered the
        credential — same pattern as miro.py, airtable.py, and
        file_upload.py:162-172. Without this, the syncer wrote zero
        SpiceDB relationships and the retriever permission filter
        (retriever.py:626) silently dropped every Asana chunk from
        search results.
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
        if self._client is None:
            return False
        try:
            await self._client.get_json("/api/1.0/users/me")
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
            retry_after = float(exc.response.headers.get("Retry-After", "60"))
            raise ConnectorRateLimitError(
                str(exc), connector_type=connector_type, retry_after=retry_after
            ) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
