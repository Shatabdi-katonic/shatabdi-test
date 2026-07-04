"""Bitbucket connector.

API: Bitbucket Cloud REST API 2.0
Auth: Basic auth (username:app_password)
Sync: Incremental (updated_on filter for issues/PRs) + full
Permissions: Repository user permissions mapped to roles

Content types indexed:
  - Repository code files (recursive src listing)
  - Issues (with comments)
  - Pull requests (with comments)

Role mapping (Bitbucket permissions):
  admin  -> owner
  write  -> editor
  read   -> viewer
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

BITBUCKET_BASE = "https://api.bitbucket.org/2.0"

PERMISSION_MAP = {
    "admin": "owner",
    "write": "editor",
    "read": "viewer",
}

DEFAULT_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".rb", ".cs",
    ".md", ".mdx", ".txt", ".rst", ".yaml", ".yml", ".json", ".toml",
    ".sh", ".bash", ".dockerfile", ".tf", ".hcl",
}

MAX_FILE_SIZE_BYTES = 1_000_000


class BitbucketConnector(ConnectorBase):
    """Bitbucket Cloud connector for code files, issues, and pull requests."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # CR-614: the Add-Knowledge wizard sends `bitbucketWorkspace` /
        # `bitbucketRepo`, not `workspace` / `repo_slug`. Accept both. This is what
        # lets a user scope the connector to their workspace — essential because
        # Bitbucket's account-wide auto-discovery endpoints all 410 for OAuth
        # tokens (see CR-612), so `/repositories/{workspace}` is the only reliable
        # path and it needs the workspace slug.
        self._workspace: str = config.get("workspace") or config.get("bitbucketWorkspace") or ""
        repo: str = config.get("repo_slug") or config.get("bitbucketRepo") or ""
        repo = repo.strip().strip("/")
        # The OAuth Bitbucket wizard only exposes a single "Repository" field, so
        # accept whatever the user puts there:
        #   "katonic/my-repo" → workspace=katonic, repo=my-repo (one repo)
        #   "katonic"          → workspace=katonic (ALL repos in it) — a bare repo
        #                        slug is useless without a workspace, so treat a
        #                        slashless value as the workspace.
        if repo and "/" in repo:
            ws_part, _, repo = repo.partition("/")
            if not self._workspace:
                self._workspace = ws_part
        elif repo and not self._workspace:
            self._workspace = repo
            repo = ""
        self._repo_slug: str = repo
        self._default_branch: str = config.get("default_branch", "main")
        self._index_code: bool = config.get("index_code", True)
        self._index_issues: bool = config.get("index_issues", True)
        self._index_prs: bool = config.get("index_pull_requests", True)
        self._code_extensions: set[str] = set(
            config.get("code_extensions", DEFAULT_CODE_EXTENSIONS)
        )
        self._client: httpx.AsyncClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with Bitbucket using OAuth Bearer token or Basic auth."""
        from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers

        access_token = credentials.get("access_token", "")
        # CR-614: wizard Basic-auth keys are bitbucketUser / bitbucketAppPassword.
        username = credentials.get("username") or credentials.get("bitbucketUser") or ""
        app_password = credentials.get("app_password") or credentials.get("bitbucketAppPassword") or ""

        if access_token:
            # OAuth 2.0 Bearer token (from OAuth flow)
            self._client = RetryClient(
                base_url=BITBUCKET_BASE,
                headers=bearer_headers(access_token),
                timeout=30.0,
                rate_limiter=self.rate_limiter,
            )
        elif username and app_password:
            # Basic auth (username + app_password)
            import base64
            b64 = base64.b64encode(f"{username}:{app_password}".encode()).decode()
            self._client = RetryClient(
                base_url=BITBUCKET_BASE,
                headers={"Authorization": f"Basic {b64}"},
                timeout=30.0,
                rate_limiter=self.rate_limiter,
            )
        else:
            raise ConnectorAuthError(
                "Bitbucket connector requires access_token (OAuth) or username + app_password",
                connector_type="bitbucket",
            )

        # Verify credentials
        resp = await self._request("GET", "/user")
        if resp.status_code == 401:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(
                "Invalid Bitbucket credentials", connector_type="bitbucket"
            )
        user = resp.json()
        self._username = user.get("username", "unknown")
        logger.info("Bitbucket authenticated as %s", self._username)

        # CR-614: only fall back to (unreliable) account-wide auto-discovery when
        # NO workspace was configured. With a workspace set, list_documents uses
        # /repositories/{workspace} directly — no discovery needed.
        if not self._workspace:
            await self._discover_repos()

    async def _discover_repos(self) -> None:
        """Discover all repos the authenticated user has access to."""
        # CR-612 (rev 4): Bitbucket's account-wide *collection* endpoints all
        # 410/404 for OAuth tokens (`/repositories/{username}`, `?role=member`,
        # `/workspaces`). Use two complementary, working strategies and merge:
        #   1. `/user/permissions/workspaces` → workspace slugs the user belongs
        #      to → `/repositories/{slug}` (lists EVERY repo in the workspace the
        #      token can read — covers membership-based access).
        #   2. `/user/permissions/repositories` (repos with an EXPLICIT user grant
        #      — supplements (1) for repos outside the user's workspaces).
        # branches: workspace listing returns mainbranch inline; explicit-perm
        # repos are resolved via the per-repo endpoint.
        self._discovered_repos = []
        repos: dict[str, str | None] = {}  # full_name -> default_branch (None = resolve later)

        # Strategy 1 — workspaces → repos
        slugs: list[str] = []
        try:
            url: str | None = "/user/permissions/workspaces?pagelen=100"
            while url:
                data = (await self._request("GET", url)).json()
                for item in data.get("values", []):
                    slug = (item.get("workspace") or {}).get("slug")
                    if slug:
                        slugs.append(slug)
                url = data.get("next", "")
                if url and url.startswith(BITBUCKET_BASE):
                    url = url[len(BITBUCKET_BASE):]
        except Exception as e:
            logger.warning("Bitbucket workspace-membership discovery failed: %s", e)

        for slug in slugs:
            try:
                url = f"/repositories/{slug}?pagelen=100"
                while url:
                    data = (await self._request("GET", url)).json()
                    for repo in data.get("values", []):
                        fn = repo.get("full_name")
                        if fn:
                            repos[fn] = (repo.get("mainbranch") or {}).get("name") or "main"
                    url = data.get("next", "")
                    if url and url.startswith(BITBUCKET_BASE):
                        url = url[len(BITBUCKET_BASE):]
            except Exception as e:
                logger.warning("Bitbucket repo list failed for workspace %s: %s", slug, e)

        # Strategy 2 — explicit repo permissions (supplement)
        try:
            url = "/user/permissions/repositories?pagelen=100"
            while url:
                data = (await self._request("GET", url)).json()
                for item in data.get("values", []):
                    fn = (item.get("repository") or {}).get("full_name")
                    if fn and fn not in repos:
                        repos[fn] = None
                url = data.get("next", "")
                if url and url.startswith(BITBUCKET_BASE):
                    url = url[len(BITBUCKET_BASE):]
        except Exception as e:
            logger.warning("Bitbucket repo permissions discovery failed: %s", e)

        # Resolve any unknown default branches via the per-repo endpoint.
        for fn, branch in repos.items():
            if branch is None:
                branch = "main"
                try:
                    r = await self._request("GET", f"/repositories/{fn}")
                    branch = (r.json().get("mainbranch") or {}).get("name") or "main"
                except Exception as e:
                    logger.debug("Bitbucket mainbranch lookup failed for %s: %s", fn, e)
            self._discovered_repos.append((fn, branch))

        logger.info(
            "Bitbucket auto-discovered %d repos across %d workspaces",
            len(self._discovered_repos),
            len(slugs),
        )

    async def _list_workspace_repos(self, workspace: str) -> list[tuple[str, str]]:
        """CR-614: list every repo in a workspace via /repositories/{workspace}.

        This is the only repo-collection endpoint that works reliably for OAuth
        tokens (the user-level discovery endpoints all 410), so an explicitly
        configured workspace is the dependable way to scope the connector.
        """
        out: list[tuple[str, str]] = []
        try:
            url: str | None = f"/repositories/{workspace}?pagelen=100"
            while url:
                data = (await self._request("GET", url)).json()
                for repo in data.get("values", []):
                    fn = repo.get("full_name")
                    if fn:
                        out.append((fn, (repo.get("mainbranch") or {}).get("name") or "main"))
                url = data.get("next", "")
                if url and url.startswith(BITBUCKET_BASE):
                    url = url[len(BITBUCKET_BASE):]
        except Exception as e:
            logger.warning("Bitbucket repo list failed for workspace %s: %s", workspace, e)
        logger.info("Bitbucket workspace %s → %d repos", workspace, len(out))
        return out

    # ------------------------------------------------------------------
    # list_documents
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List code files, issues, and pull requests."""
        assert self._client is not None

        # Target selection (priority order):
        #  1. explicit workspace + repo  → that one repo
        #  2. CR-614: explicit workspace only → ALL repos in that workspace via
        #     `/repositories/{workspace}` (the one collection endpoint that works
        #     for OAuth tokens; the user-level discovery endpoints all 410).
        #  3. else → whatever auto-discovery found (best-effort).
        # The per-repo list methods build external_ids from self._workspace/
        # self._repo_slug and walk /src/{default_branch}/, so set all three per
        # repo below (repos can default to master, not main).
        if self._workspace and self._repo_slug:
            targets: list[tuple[str, str]] = [
                (f"{self._workspace}/{self._repo_slug}", self._default_branch)
            ]
        elif self._workspace:
            targets = await self._list_workspace_repos(self._workspace)
        else:
            targets = list(getattr(self, "_discovered_repos", []))

        for full_name, branch in targets:
            ws, _, slug = full_name.partition("/")
            if not ws or not slug:
                continue
            self._workspace, self._repo_slug, self._default_branch = ws, slug, branch
            repo_path = f"/repositories/{ws}/{slug}"

            if self._index_code:
                async for doc in self._list_code_files(repo_path):
                    yield doc

            if self._index_issues:
                async for doc in self._list_issues(repo_path, since):
                    yield doc

            if self._index_prs:
                async for doc in self._list_pull_requests(repo_path, since):
                    yield doc

    async def _list_code_files(self, repo_path: str) -> AsyncIterator[DocumentMetadata]:
        """Recursively list code files from the repository source tree."""
        url = f"{repo_path}/src/{self._default_branch}/"
        async for doc in self._walk_src_tree(url, []):
            yield doc

    async def _walk_src_tree(
        self, url: str, _results: list
    ) -> AsyncIterator[DocumentMetadata]:
        """Walk the Bitbucket src tree recursively, yielding code file metadata."""
        page_url: str | None = url

        while page_url:
            resp = await self._request("GET", page_url)
            data = resp.json()

            for entry in data.get("values", []):
                entry_type = entry.get("type", "")
                path = entry.get("path", "")

                if entry_type == "commit_directory":
                    # Recurse into subdirectories
                    sub_url = entry.get("links", {}).get("self", {}).get("href", "")
                    if sub_url:
                        # Convert absolute URL to relative
                        if sub_url.startswith(BITBUCKET_BASE):
                            sub_url = sub_url[len(BITBUCKET_BASE):]
                        async for doc in self._walk_src_tree(sub_url, _results):
                            yield doc

                elif entry_type == "commit_file":
                    ext = ""
                    dot_idx = path.rfind(".")
                    if dot_idx >= 0:
                        ext = path[dot_idx:]
                    if ext.lower() not in self._code_extensions:
                        continue

                    size = entry.get("size", 0)
                    if size > MAX_FILE_SIZE_BYTES:
                        continue

                    yield DocumentMetadata(
                        external_id=f"bitbucket:{self._workspace}:{self._repo_slug}:code:{path}",
                        title=path,
                        url=entry.get("links", {}).get("html", {}).get("href"),
                        content_type=_guess_content_type(ext),
                        size_bytes=size,
                        metadata={
                            "type": "code",
                            "workspace": self._workspace,
                            "repo": self._repo_slug,
                            "source": "bitbucket",
                        },
                    )

            page_url = data.get("next")
            if page_url and page_url.startswith(BITBUCKET_BASE):
                page_url = page_url[len(BITBUCKET_BASE):]

    async def _list_issues(
        self, repo_path: str, since: datetime | None
    ) -> AsyncIterator[DocumentMetadata]:
        """List repository issues with pagination."""
        page_url: str | None = f"{repo_path}/issues"
        params: dict[str, str] = {}
        if since:
            params["q"] = f'updated_on>"{since.strftime("%Y-%m-%dT%H:%M:%S")}"'

        while page_url:
            try:
                resp = await self._request("GET", page_url, params=params)
            except Exception as exc:  # noqa: BLE001
                # CR-616: the issue tracker is disabled by default on Bitbucket
                # repos → /issues returns 404 (or 403). That must NOT abort the
                # sync, or already-discovered code files never get embedded
                # (README stuck "pending", 0 chunks). Skip issues gracefully.
                import httpx
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 404):
                    logger.info("Bitbucket issues unavailable for %s (tracker off?): %s", repo_path, exc)
                    return
                raise
            data = resp.json()
            params = {}  # only on first request

            for issue in data.get("values", []):
                issue_id = issue.get("id", "")
                yield DocumentMetadata(
                    external_id=f"bitbucket:{self._workspace}:{self._repo_slug}:issue:{issue_id}",
                    title=f"[Issue #{issue_id}] {issue.get('title', '')}",
                    url=issue.get("links", {}).get("html", {}).get("href"),
                    content_type="text/markdown",
                    author=issue.get("reporter", {}).get("username"),
                    modified_at=_parse_dt(issue.get("updated_on", "")),
                    metadata={
                        "type": "issue",
                        "state": issue.get("state"),
                        "priority": issue.get("priority"),
                        "source": "bitbucket",
                    },
                )

            next_url = data.get("next")
            if next_url and next_url.startswith(BITBUCKET_BASE):
                page_url = next_url[len(BITBUCKET_BASE):]
            elif next_url:
                page_url = next_url
            else:
                page_url = None

    async def _list_pull_requests(
        self, repo_path: str, since: datetime | None
    ) -> AsyncIterator[DocumentMetadata]:
        """List repository pull requests with pagination."""
        page_url: str | None = f"{repo_path}/pullrequests"
        params: dict[str, str] = {}
        if since:
            params["q"] = f'updated_on>"{since.strftime("%Y-%m-%dT%H:%M:%S")}"'

        while page_url:
            try:
                resp = await self._request("GET", page_url, params=params)
            except Exception as exc:  # noqa: BLE001
                # CR-616: tolerate pull requests being unavailable (403/404) so a
                # missing/disabled PR feature doesn't abort indexing of code files.
                import httpx
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 404):
                    logger.info("Bitbucket pull requests unavailable for %s: %s", repo_path, exc)
                    return
                raise
            data = resp.json()
            params = {}

            for pr in data.get("values", []):
                pr_id = pr.get("id", "")
                yield DocumentMetadata(
                    external_id=f"bitbucket:{self._workspace}:{self._repo_slug}:pr:{pr_id}",
                    title=f"[PR #{pr_id}] {pr.get('title', '')}",
                    url=pr.get("links", {}).get("html", {}).get("href"),
                    content_type="text/markdown",
                    author=pr.get("author", {}).get("username"),
                    modified_at=_parse_dt(pr.get("updated_on", "")),
                    metadata={
                        "type": "pull_request",
                        "state": pr.get("state"),
                        "source": "bitbucket",
                    },
                )

            next_url = data.get("next")
            if next_url and next_url.startswith(BITBUCKET_BASE):
                page_url = next_url[len(BITBUCKET_BASE):]
            elif next_url:
                page_url = next_url
            else:
                page_url = None

    # ------------------------------------------------------------------
    # fetch_document
    # ------------------------------------------------------------------

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch document content by external ID."""
        assert self._client is not None
        parts = doc_id.split(":")
        if len(parts) < 5 or parts[0] != "bitbucket":
            raise ValueError(f"Invalid Bitbucket doc_id format: {doc_id}")

        workspace = parts[1]
        repo = parts[2]
        doc_type = parts[3]
        identifier = ":".join(parts[4:])

        if doc_type == "code":
            return await self._fetch_code_file(workspace, repo, identifier)
        elif doc_type == "issue":
            return await self._fetch_issue(workspace, repo, identifier)
        elif doc_type == "pr":
            return await self._fetch_pull_request(workspace, repo, identifier)
        else:
            raise ValueError(f"Unknown Bitbucket doc type: {doc_type}")

    async def _fetch_code_file(self, workspace: str, repo: str, path: str) -> RawDocument:
        """Fetch raw file content from the repository."""
        resp = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo}/src/{self._default_branch}/{path}",
        )
        content_type = resp.headers.get("Content-Type", "text/plain")
        return RawDocument(
            external_id=f"bitbucket:{workspace}:{repo}:code:{path}",
            content=resp.content,
            content_type=content_type,
            metadata={"filename": path.split("/")[-1], "type": "code"},
        )

    async def _fetch_issue(self, workspace: str, repo: str, issue_id: str) -> RawDocument:
        """Fetch an issue with its comments."""
        resp = await self._request(
            "GET", f"/repositories/{workspace}/{repo}/issues/{issue_id}"
        )
        issue = resp.json()

        lines = [
            f"# Issue #{issue_id}: {issue.get('title', '')}",
            f"**State:** {issue.get('state', '')}",
            f"**Priority:** {issue.get('priority', '')}",
            f"**Reporter:** @{issue.get('reporter', {}).get('username', 'unknown')}",
            f"**Created:** {issue.get('created_on', '')}",
            "",
            (issue.get("content", {}).get("raw") or "(no description)"),
        ]

        # Fetch comments
        try:
            comments_resp = await self._request(
                "GET", f"/repositories/{workspace}/{repo}/issues/{issue_id}/comments"
            )
            comments_data = comments_resp.json()
            comments = comments_data.get("values", [])
            if comments:
                lines.append("\n---\n## Comments\n")
                for c in comments:
                    author = c.get("user", {}).get("username", "unknown")
                    lines.append(f"### @{author} ({c.get('created_on', '')})")
                    lines.append(c.get("content", {}).get("raw", "") or "(empty)")
                    lines.append("")
        except Exception:
            pass

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=f"bitbucket:{workspace}:{repo}:issue:{issue_id}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"issue-{issue_id}.md", "type": "issue"},
        )

    async def _fetch_pull_request(
        self, workspace: str, repo: str, pr_id: str
    ) -> RawDocument:
        """Fetch a pull request with its comments."""
        resp = await self._request(
            "GET", f"/repositories/{workspace}/{repo}/pullrequests/{pr_id}"
        )
        pr = resp.json()

        source_branch = pr.get("source", {}).get("branch", {}).get("name", "")
        dest_branch = pr.get("destination", {}).get("branch", {}).get("name", "")

        lines = [
            f"# PR #{pr_id}: {pr.get('title', '')}",
            f"**State:** {pr.get('state', '')}",
            f"**Author:** @{pr.get('author', {}).get('username', 'unknown')}",
            f"**Branches:** {source_branch} -> {dest_branch}",
            f"**Created:** {pr.get('created_on', '')}",
            "",
            (pr.get("description") or "(no description)"),
        ]

        # Fetch comments
        try:
            comments_resp = await self._request(
                "GET", f"/repositories/{workspace}/{repo}/pullrequests/{pr_id}/comments"
            )
            comments_data = comments_resp.json()
            comments = comments_data.get("values", [])
            if comments:
                lines.append("\n---\n## Comments\n")
                for c in comments:
                    author = c.get("user", {}).get("username", "unknown")
                    lines.append(f"### @{author} ({c.get('created_on', '')})")
                    lines.append(c.get("content", {}).get("raw", "") or "(empty)")
                    lines.append("")
        except Exception:
            pass

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=f"bitbucket:{workspace}:{repo}:pr:{pr_id}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"pr-{pr_id}.md", "type": "pull_request"},
        )

    # ------------------------------------------------------------------
    # get_permissions
    # ------------------------------------------------------------------

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get repository user permissions mapped to roles."""
        assert self._client is not None
        parts = doc_id.split(":")
        if len(parts) < 3:
            return []

        workspace = parts[1]
        repo = parts[2]
        entries: list[PermissionEntry] = []

        page_url: str | None = (
            f"/repositories/{workspace}/{repo}/permissions-config/users"
        )

        while page_url:
            try:
                resp = await self._request("GET", page_url)
                data = resp.json()

                for perm in data.get("values", []):
                    user = perm.get("user", {})
                    permission = perm.get("permission", "read")
                    entries.append(
                        PermissionEntry(
                            subject_type="user",
                            subject_id=user.get("username", str(user.get("uuid", ""))),
                            relation=PERMISSION_MAP.get(permission, "viewer"),
                        )
                    )

                next_url = data.get("next")
                if next_url and next_url.startswith(BITBUCKET_BASE):
                    page_url = next_url[len(BITBUCKET_BASE):]
                elif next_url:
                    page_url = next_url
                else:
                    page_url = None
            except Exception as e:
                logger.warning(
                    "Failed to get permissions for %s/%s: %s", workspace, repo, e
                )
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
            await self._client.close()

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
                    f"Bitbucket request timed out: {exc}", connector_type="bitbucket"
                ) from exc

            if resp.status_code == 401:
                raise ConnectorAuthError(
                    "Bitbucket auth failed (401)", connector_type="bitbucket"
                )

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "10"))
                if attempt < 3:
                    logger.warning("Bitbucket rate limited, sleeping %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                raise ConnectorRateLimitError(
                    "Bitbucket rate limit exceeded",
                    connector_type="bitbucket",
                    retry_after=retry_after,
                )

            if resp.status_code >= 500:
                if attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                raise ConnectorTransientError(
                    f"Bitbucket server error {resp.status_code}",
                    connector_type="bitbucket",
                )

            resp.raise_for_status()
            return resp

        raise ConnectorTransientError(
            "Bitbucket max retries exceeded", connector_type="bitbucket"
        )


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime from Bitbucket API."""
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
