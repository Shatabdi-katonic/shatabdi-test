"""GitLab connector.

API: GitLab REST API v4
Auth: Private-Token header
Sync: Incremental (updated_after filter) + full
Permissions: Project member access levels mapped to roles

Content types indexed:
  - Repository code files (recursive tree listing)
  - Issues (with notes/comments)
  - Merge requests (with notes/comments)

Role mapping (GitLab access levels):
  50 (Owner)     -> owner
  40 (Maintainer)-> owner
  30 (Developer) -> editor
  20 (Reporter)  -> viewer
  10 (Guest)     -> viewer
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

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

ACCESS_LEVEL_MAP = {
    50: "owner",      # Owner
    40: "owner",      # Maintainer
    30: "editor",     # Developer
    20: "viewer",     # Reporter
    10: "viewer",     # Guest
}

DEFAULT_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".rb", ".cs",
    ".md", ".mdx", ".txt", ".rst", ".yaml", ".yml", ".json", ".toml",
    ".sh", ".bash", ".dockerfile", ".tf", ".hcl",
}

MAX_FILE_SIZE_BYTES = 1_000_000


class GitLabConnector(ConnectorBase):
    """GitLab connector for code files, issues, and merge requests."""

    CONFIG_SCHEMA = [
        ConfigField(
            key="base_url",
            label="GitLab URL",
            type="text",
            required=False,
            default="https://gitlab.com",
            placeholder="https://gitlab.com",
            help_text="Change only for self-hosted GitLab instances.",
        ),
        ConfigField(
            key="project_ids",
            label="Project IDs / paths",
            type="text",
            required=False,
            placeholder="e.g. 12345678 or group/project (comma separated)",
            help_text="Specific projects to sync. Leave blank to sync every project the user is a member of.",
        ),
    ]

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._base_url: str = config.get("base_url", "https://gitlab.com").rstrip("/")
        # Accept project_ids as a list OR a comma-separated string from the UI
        raw_projects = config.get("project_ids", [])
        if isinstance(raw_projects, str):
            raw_projects = [p.strip() for p in raw_projects.split(",") if p.strip()]
        self._project_ids: list[int | str] = raw_projects
        self._index_code: bool = config.get("index_code", True)
        self._index_issues: bool = config.get("index_issues", True)
        self._index_merge_requests: bool = config.get("index_merge_requests", True)
        self._default_branch: str = config.get("default_branch", "main")
        self._code_extensions: set[str] = set(
            config.get("code_extensions", DEFAULT_CODE_EXTENSIONS)
        )
        self._client: httpx.AsyncClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with a GitLab private token."""
        token = credentials.get("private_token", "") or credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError(
                "GitLab connector requires private_token", connector_type="gitlab"
            )

        from platform_knowledge_engine.connectors._utils.http_client import RetryClient
        self._client = RetryClient(
            base_url=f"{self._base_url}/api/v4",
            headers={"Private-Token": token},
            timeout=30.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify token
        resp = await self._request("GET", "/user")
        if resp.status_code == 401:
            await self._client.aclose()
            self._client = None
            raise ConnectorAuthError("Invalid GitLab private token", connector_type="gitlab")

        user = resp.json()
        logger.info("GitLab authenticated as %s", user.get("username", "unknown"))

        # Fallback: when no project_ids are configured, list every project the
        # user is a member of. Without this, list_documents silently yields 0
        # documents because the for-loop iterates over an empty list.
        if not self._project_ids:
            await self._discover_user_projects()

        logger.info(
            "gitlab_auth_ready user=%s projects=%d",
            user.get("username", "?"),
            len(self._project_ids),
        )

    async def _discover_user_projects(self) -> None:
        """List every project the authenticated user has access to."""
        page = 1
        while True:
            resp = await self._request(
                "GET",
                "/projects",
                params={
                    "membership": "true",
                    "per_page": "100",
                    "page": str(page),
                    "order_by": "last_activity_at",
                    "simple": "true",
                },
            )
            items = resp.json()
            if not isinstance(items, list) or not items:
                break
            for proj in items:
                self._project_ids.append(proj["id"])
            if len(items) < 100:
                break
            page += 1
        logger.info("gitlab_discover_user_projects count=%d", len(self._project_ids))

    # ------------------------------------------------------------------
    # list_documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List code files, issues, and merge requests across configured projects."""
        assert self._client is not None

        for project_id in self._project_ids:
            try:
                if self._index_code:
                    async for doc in self._list_code_files(project_id):
                        yield doc
                if self._index_issues:
                    async for doc in self._list_issues(project_id, since):
                        yield doc
                if self._index_merge_requests:
                    async for doc in self._list_merge_requests(project_id, since):
                        yield doc
            except Exception as e:
                logger.error("Failed to list docs from project %s: %s", project_id, e)

    async def _list_code_files(self, project_id: int | str) -> AsyncIterator[DocumentMetadata]:
        """List indexable code files from the repository tree."""
        page = 1
        while True:
            resp = await self._request(
                "GET",
                f"/projects/{project_id}/repository/tree",
                params={
                    "recursive": "true",
                    "per_page": "100",
                    "page": str(page),
                    "ref": self._default_branch,
                },
            )
            items = resp.json()
            if not isinstance(items, list) or not items:
                break

            for item in items:
                if item.get("type") != "blob":
                    continue
                path = item.get("path", "")
                ext = ""
                dot_idx = path.rfind(".")
                if dot_idx >= 0:
                    ext = path[dot_idx:]
                if ext.lower() not in self._code_extensions:
                    continue

                yield DocumentMetadata(
                    external_id=f"gitlab:{project_id}:code:{path}",
                    title=path,
                    url=f"{self._base_url}/projects/{project_id}/-/blob/{self._default_branch}/{path}",
                    content_type=_guess_content_type(ext),
                    metadata={
                        "type": "code",
                        "project_id": str(project_id),
                        "sha": item.get("id", ""),
                        "source": "gitlab",
                    },
                )

            if len(items) < 100:
                break
            page += 1

    async def _list_issues(
        self, project_id: int | str, since: datetime | None
    ) -> AsyncIterator[DocumentMetadata]:
        """List project issues with pagination."""
        page = 1
        while True:
            params: dict[str, str] = {
                "per_page": "100",
                "page": str(page),
                "order_by": "updated_at",
                "sort": "desc",
            }
            if since:
                params["updated_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

            resp = await self._request(
                "GET", f"/projects/{project_id}/issues", params=params
            )
            issues = resp.json()
            if not isinstance(issues, list) or not issues:
                break

            for issue in issues:
                yield DocumentMetadata(
                    external_id=f"gitlab:{project_id}:issue:{issue['iid']}",
                    title=f"[Issue #{issue['iid']}] {issue.get('title', '')}",
                    url=issue.get("web_url"),
                    content_type="text/markdown",
                    author=issue.get("author", {}).get("username"),
                    modified_at=_parse_dt(issue.get("updated_at", "")),
                    metadata={
                        "type": "issue",
                        "project_id": str(project_id),
                        "state": issue.get("state"),
                        "source": "gitlab",
                    },
                )

            if len(issues) < 100:
                break
            page += 1

    async def _list_merge_requests(
        self, project_id: int | str, since: datetime | None
    ) -> AsyncIterator[DocumentMetadata]:
        """List project merge requests with pagination."""
        page = 1
        while True:
            params: dict[str, str] = {
                "per_page": "100",
                "page": str(page),
                "order_by": "updated_at",
                "sort": "desc",
            }
            if since:
                params["updated_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

            resp = await self._request(
                "GET", f"/projects/{project_id}/merge_requests", params=params
            )
            mrs = resp.json()
            if not isinstance(mrs, list) or not mrs:
                break

            for mr in mrs:
                yield DocumentMetadata(
                    external_id=f"gitlab:{project_id}:mr:{mr['iid']}",
                    title=f"[MR !{mr['iid']}] {mr.get('title', '')}",
                    url=mr.get("web_url"),
                    content_type="text/markdown",
                    author=mr.get("author", {}).get("username"),
                    modified_at=_parse_dt(mr.get("updated_at", "")),
                    metadata={
                        "type": "merge_request",
                        "project_id": str(project_id),
                        "state": mr.get("state"),
                        "source": "gitlab",
                    },
                )

            if len(mrs) < 100:
                break
            page += 1

    # ------------------------------------------------------------------
    # fetch_document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch document content by external ID."""
        assert self._client is not None
        parts = doc_id.split(":")
        if len(parts) < 4 or parts[0] != "gitlab":
            raise ValueError(f"Invalid GitLab doc_id format: {doc_id}")

        project_id = parts[1]
        doc_type = parts[2]
        identifier = ":".join(parts[3:])  # path may contain colons

        if doc_type == "code":
            return await self._fetch_code_file(project_id, identifier)
        elif doc_type == "issue":
            return await self._fetch_issue(project_id, int(identifier))
        elif doc_type == "mr":
            return await self._fetch_merge_request(project_id, int(identifier))
        else:
            raise ValueError(f"Unknown GitLab doc type: {doc_type}")

    async def _fetch_code_file(self, project_id: str, path: str) -> RawDocument:
        """Fetch a raw file from the repository."""
        encoded_path = quote(path, safe="")
        resp = await self._request(
            "GET",
            f"/projects/{project_id}/repository/files/{encoded_path}/raw",
            params={"ref": self._default_branch},
        )
        content_type = resp.headers.get("Content-Type", "text/plain")
        return RawDocument(
            external_id=f"gitlab:{project_id}:code:{path}",
            content=resp.content,
            content_type=content_type,
            metadata={"filename": path.split("/")[-1], "type": "code"},
        )

    async def _fetch_issue(self, project_id: str, iid: int) -> RawDocument:
        """Fetch an issue with its notes."""
        resp = await self._request("GET", f"/projects/{project_id}/issues/{iid}")
        issue = resp.json()

        lines = [
            f"# Issue #{iid}: {issue.get('title', '')}",
            f"**State:** {issue.get('state', '')}",
            f"**Author:** @{issue.get('author', {}).get('username', 'unknown')}",
            f"**Created:** {issue.get('created_at', '')}",
            f"**Labels:** {', '.join(issue.get('labels', []))}",
            "",
            issue.get("description") or "(no description)",
        ]

        # Fetch notes
        try:
            notes_resp = await self._request(
                "GET",
                f"/projects/{project_id}/issues/{iid}/notes",
                params={"per_page": "100", "sort": "asc"},
            )
            notes = notes_resp.json()
            if notes and isinstance(notes, list):
                lines.append("\n---\n## Comments\n")
                for note in notes:
                    if note.get("system"):
                        continue
                    author = note.get("author", {}).get("username", "unknown")
                    lines.append(f"### @{author} ({note.get('created_at', '')})")
                    lines.append(note.get("body", "") or "(empty)")
                    lines.append("")
        except Exception:
            pass

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=f"gitlab:{project_id}:issue:{iid}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"issue-{iid}.md", "type": "issue"},
        )

    async def _fetch_merge_request(self, project_id: str, iid: int) -> RawDocument:
        """Fetch a merge request with its notes."""
        resp = await self._request("GET", f"/projects/{project_id}/merge_requests/{iid}")
        mr = resp.json()

        lines = [
            f"# MR !{iid}: {mr.get('title', '')}",
            f"**State:** {mr.get('state', '')}",
            f"**Author:** @{mr.get('author', {}).get('username', 'unknown')}",
            f"**Source:** {mr.get('source_branch', '')} -> {mr.get('target_branch', '')}",
            f"**Created:** {mr.get('created_at', '')}",
            "",
            mr.get("description") or "(no description)",
        ]

        # Fetch notes
        try:
            notes_resp = await self._request(
                "GET",
                f"/projects/{project_id}/merge_requests/{iid}/notes",
                params={"per_page": "100", "sort": "asc"},
            )
            notes = notes_resp.json()
            if notes and isinstance(notes, list):
                lines.append("\n---\n## Comments\n")
                for note in notes:
                    if note.get("system"):
                        continue
                    author = note.get("author", {}).get("username", "unknown")
                    lines.append(f"### @{author} ({note.get('created_at', '')})")
                    lines.append(note.get("body", "") or "(empty)")
                    lines.append("")
        except Exception:
            pass

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=f"gitlab:{project_id}:mr:{iid}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"mr-{iid}.md", "type": "merge_request"},
        )

    # ------------------------------------------------------------------
    # get_permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get project member permissions mapped to roles."""
        assert self._client is not None
        parts = doc_id.split(":")
        if len(parts) < 3:
            return []

        project_id = parts[1]
        entries: list[PermissionEntry] = []

        page = 1
        while True:
            try:
                resp = await self._request(
                    "GET",
                    f"/projects/{project_id}/members/all",
                    params={"per_page": "100", "page": str(page)},
                )
                members = resp.json()
                if not isinstance(members, list) or not members:
                    break

                for member in members:
                    access_level = member.get("access_level", 10)
                    role = ACCESS_LEVEL_MAP.get(access_level, "viewer")
                    entries.append(
                        PermissionEntry(
                            subject_type="user",
                            subject_id=member.get("username", str(member.get("id", ""))),
                            relation=role,
                        )
                    )

                if len(members) < 100:
                    break
                page += 1
            except Exception as e:
                logger.warning("Failed to get members for project %s: %s", project_id, e)
                break

        return entries

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            resp = await self._request("GET", "/user")
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
                    f"GitLab request timed out: {exc}", connector_type="gitlab"
                ) from exc

            if resp.status_code == 401:
                raise ConnectorAuthError("GitLab auth failed (401)", connector_type="gitlab")

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "10"))
                if attempt < 3:
                    logger.warning("GitLab rate limited, sleeping %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                raise ConnectorRateLimitError(
                    "GitLab rate limit exceeded",
                    connector_type="gitlab",
                    retry_after=retry_after,
                )

            if resp.status_code >= 500:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"GitLab server error {resp.status_code}", connector_type="gitlab"
                )

            resp.raise_for_status()
            return resp

        raise ConnectorTransientError("GitLab max retries exceeded", connector_type="gitlab")


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime from GitLab API."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _guess_content_type(ext: str) -> str:
    """Guess content type from file extension."""
    ext = ext.lstrip(".").lower()
    mapping = {
        "md": "text/markdown", "mdx": "text/markdown", "txt": "text/plain",
        "rst": "text/x-rst", "py": "text/x-python", "js": "text/javascript",
        "ts": "text/typescript", "java": "text/x-java", "go": "text/x-go",
        "rs": "text/x-rust", "rb": "text/x-ruby", "cs": "text/x-csharp",
        "yaml": "text/yaml", "yml": "text/yaml", "json": "application/json",
        "toml": "text/toml", "sh": "text/x-sh", "tf": "text/x-terraform",
    }
    return mapping.get(ext, "text/plain")
