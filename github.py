"""GitHub connector.

API: GitHub REST API v3 + GraphQL v4
Auth: OAuth 2.0 (GitHub App) or Personal Access Token
Sync: Incremental (pushed_at / updated_at filter) + full
Permissions: Repository collaborators + team memberships

Content types indexed:
  - Repository README and docs (markdown files)
  - Issues (with comments)
  - Pull requests (with review comments)
  - Wiki pages
  - Code files (configurable extensions)

Role mapping (spec section 15):
  admin       -> owner
  maintain    -> editor
  push/write  -> editor
  triage      -> viewer
  pull/read   -> viewer
"""

from __future__ import annotations

import base64
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

GITHUB_API = "https://api.github.com"

# File extensions to index by default (documentation-oriented)
DEFAULT_INDEXABLE_EXTENSIONS = {
    ".md",
    ".mdx",
    ".txt",
    ".rst",
    ".adoc",
    ".asciidoc",
    ".py",
    ".js",
    ".ts",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".cs",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
}

MAX_FILE_SIZE_BYTES = 1_000_000  # Skip files > 1MB

ROLE_MAP = {
    "admin": "owner",
    "maintain": "editor",
    "push": "editor",
    "write": "editor",
    "triage": "viewer",
    "pull": "viewer",
    "read": "viewer",
}


class GitHubConnector(ConnectorBase):
    """GitHub connector for repositories, issues, PRs, and wiki pages."""

    CONFIG_SCHEMA = [
        ConfigField(
            key="githubUrl",
            label="Repository URL",
            type="text",
            required=False,
            placeholder="https://github.com/owner/repo",
            help_text="Single repository to sync. Leave blank to sync all repos accessible to the OAuth user (or to the organization if set below).",
        ),
        ConfigField(
            key="organization",
            label="Organization (optional)",
            type="text",
            required=False,
            placeholder="e.g. anthropic",
            help_text="Sync every repository in this org. Ignored if Repository URL is set.",
        ),
    ]

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._repos: list[str] = config.get("repositories", [])  # ["owner/repo", ...]
        # Frontend field: githubUrl — parse owner/repo from URL
        github_url = config.get("githubUrl", "")
        if github_url and not self._repos:
            parsed = _parse_github_url(github_url)
            if parsed:
                self._repos = [parsed]
        self._org: str = config.get("organization", "")
        self._index_issues: bool = config.get("index_issues", True)
        self._index_prs: bool = config.get("index_pull_requests", True)
        self._index_wiki: bool = config.get("index_wiki", False)
        self._index_code: bool = config.get("index_code", True)
        self._code_extensions: set[str] = set(
            config.get("code_extensions", DEFAULT_INDEXABLE_EXTENSIONS)
        )
        self._code_paths: list[str] = config.get("code_paths", [])  # restrict to subdirs
        # Frontend field: githubToken as PAT fallback
        self._github_token: str = config.get("githubToken", "")
        self._client: RetryClient | None = None
        self._authenticated_user: str = ""

    async def authenticate(self, credentials: dict) -> None:
        token = (
            credentials.get("access_token", "")
            or credentials.get("pat", "")
            or credentials.get("githubToken", "")
            or self._github_token
        )
        if not token:
            raise ConnectorAuthError(
                "GitHub connector requires access_token, pat, or githubToken"
            )

        self._client = RetryClient(
            base_url=GITHUB_API,
            headers={
                **bearer_headers(token),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        # Verify token and get authenticated user
        user = await self._client.get_json("/user")
        self._authenticated_user = user.get("login", "unknown")
        logger.info("GitHub authenticated as %s", self._authenticated_user)

        # If org is specified but no repos, discover all repos in the org
        if self._org and not self._repos:
            await self._discover_org_repos()

        # Fallback: when neither a specific repo nor an org is configured,
        # discover every repo the authenticated user has access to. Without
        # this, list_documents silently yields 0 documents — the most common
        # "Synced but empty" OAuth failure mode.
        if not self._repos and not self._org:
            await self._discover_user_repos()

        logger.info(
            "github_auth_ready user=%s repos=%d org=%r",
            self._authenticated_user,
            len(self._repos),
            self._org,
        )

    async def _discover_org_repos(self) -> None:
        """List all repos in the organization the token has access to."""
        page = 1
        while True:
            repos = await self._client.get_json(
                f"/orgs/{self._org}/repos",
                params={"per_page": "100", "page": str(page), "type": "all"},
            )
            if not repos:
                break
            for repo in repos:
                self._repos.append(repo["full_name"])
            if len(repos) < 100:
                break
            page += 1
        logger.info("Discovered %d repos in org %s", len(self._repos), self._org)

    async def _discover_user_repos(self) -> None:
        """List every repo the authenticated user can read (owned, collaborator, org member).

        GitHub's /user/repos returns up to 100 per page and supports the
        `affiliation` filter. We request all three affiliations so the listing
        covers personal repos, collaborator access, and org repos without
        needing the user to name them explicitly.
        """
        page = 1
        while True:
            repos = await self._client.get_json(
                "/user/repos",
                params={
                    "per_page": "100",
                    "page": str(page),
                    "affiliation": "owner,collaborator,organization_member",
                    "sort": "updated",
                },
            )
            if not repos:
                break
            for repo in repos:
                self._repos.append(repo["full_name"])
            if len(repos) < 100:
                break
            page += 1
        logger.info(
            "github_discover_user_repos user=%s count=%d",
            self._authenticated_user,
            len(self._repos),
        )

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List documents across all configured repositories."""
        assert self._client is not None

        for repo_full_name in self._repos:
            try:
                async for doc in self._list_repo_documents(repo_full_name, since):
                    yield doc
            except Exception as e:
                logger.error("Failed to list docs from %s: %s", repo_full_name, e)
                raise

    async def _list_repo_documents(
        self,
        repo: str,
        since: datetime | None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all indexable content from a single repository."""
        assert self._client is not None
        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None

        # 1. Code files (default branch)
        if self._index_code:
            async for doc in self._list_code_files(repo):
                yield doc

        # 2. Issues
        if self._index_issues:
            params: dict = {
                "state": "all",
                "per_page": "100",
                "sort": "updated",
                "direction": "desc",
            }
            if since_str:
                params["since"] = since_str
            page = 1
            while True:
                params["page"] = str(page)
                issues = await self._client.get_json(f"/repos/{repo}/issues", params=params)
                if not issues:
                    break
                for issue in issues:
                    # Skip pull requests (GitHub API lists PRs as issues too)
                    if "pull_request" in issue:
                        continue
                    yield DocumentMetadata(
                        external_id=f"{repo}/issues/{issue['number']}",
                        title=f"[Issue #{issue['number']}] {issue['title']}",
                        url=issue.get("html_url"),
                        content_type="text/markdown",
                        author=issue.get("user", {}).get("login"),
                        modified_at=_parse_dt(issue.get("updated_at", "")),
                        metadata={"type": "issue", "repo": repo, "state": issue.get("state")},
                    )
                if len(issues) < 100:
                    break
                page += 1

        # 3. Pull requests
        if self._index_prs:
            params = {"state": "all", "per_page": "100", "sort": "updated", "direction": "desc"}
            page = 1
            while True:
                params["page"] = str(page)
                prs = await self._client.get_json(f"/repos/{repo}/pulls", params=params)
                if not prs:
                    break
                for pr in prs:
                    updated = _parse_dt(pr.get("updated_at", ""))
                    if since and updated < since:
                        # PRs sorted by updated desc; stop early
                        return
                    yield DocumentMetadata(
                        external_id=f"{repo}/pulls/{pr['number']}",
                        title=f"[PR #{pr['number']}] {pr['title']}",
                        url=pr.get("html_url"),
                        content_type="text/markdown",
                        author=pr.get("user", {}).get("login"),
                        modified_at=updated,
                        metadata={"type": "pull_request", "repo": repo, "state": pr.get("state")},
                    )
                if len(prs) < 100:
                    break
                page += 1

    async def _list_code_files(self, repo: str) -> AsyncIterator[DocumentMetadata]:
        """List indexable code/doc files from the default branch tree."""
        assert self._client is not None
        try:
            # Get the default branch SHA
            repo_info = await self._client.get_json(f"/repos/{repo}")
            default_branch = repo_info.get("default_branch", "main")

            # Get recursive tree
            tree = await self._client.get_json(
                f"/repos/{repo}/git/trees/{default_branch}",
                params={"recursive": "1"},
            )

            for item in tree.get("tree", []):
                if item["type"] != "blob":
                    continue
                path = item["path"]
                size = item.get("size", 0)

                # Filter by extension
                ext = ""
                dot_idx = path.rfind(".")
                if dot_idx >= 0:
                    ext = path[dot_idx:]
                if ext.lower() not in self._code_extensions:
                    continue

                # Filter by path prefix if configured
                if self._code_paths and not any(path.startswith(p) for p in self._code_paths):
                    continue

                # Skip large files
                if size > MAX_FILE_SIZE_BYTES:
                    continue

                yield DocumentMetadata(
                    external_id=f"{repo}/blob/{default_branch}/{path}",
                    title=path,
                    url=f"https://github.com/{repo}/blob/{default_branch}/{path}",
                    content_type=_guess_content_type(ext),
                    size_bytes=size,
                    metadata={"type": "code", "repo": repo, "path": path, "sha": item["sha"]},
                )
        except Exception as e:
            logger.warning("Failed to list code files for %s: %s", repo, e)

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch document content by external_id."""
        assert self._client is not None

        # Parse doc_id format: "owner/repo/type/..."
        parts = doc_id.split("/")

        if len(parts) >= 4 and parts[2] == "issues":
            return await self._fetch_issue(f"{parts[0]}/{parts[1]}", int(parts[3]))
        elif len(parts) >= 4 and parts[2] == "pulls":
            return await self._fetch_pr(f"{parts[0]}/{parts[1]}", int(parts[3]))
        elif len(parts) >= 4 and parts[2] == "blob":
            repo = f"{parts[0]}/{parts[1]}"
            ref = parts[3]
            path = "/".join(parts[4:])
            return await self._fetch_file(repo, ref, path)
        else:
            raise ValueError(f"Unknown document ID format: {doc_id}")

    async def _fetch_issue(self, repo: str, number: int) -> RawDocument:
        """Fetch issue with all comments as markdown."""
        assert self._client is not None
        issue = await self._client.get_json(f"/repos/{repo}/issues/{number}")

        lines = [
            f"# Issue #{number}: {issue['title']}",
            f"**State:** {issue.get('state', 'unknown')}  ",
            f"**Author:** @{issue.get('user', {}).get('login', 'unknown')}  ",
            f"**Created:** {issue.get('created_at', '')}  ",
            f"**Labels:** {', '.join(l['name'] for l in issue.get('labels', []))}",
            "",
            issue.get("body") or "(no description)",
        ]

        # Fetch comments (paginated — issues can have >100 comments)
        if issue.get("comments", 0) > 0:
            lines.append("\n---\n## Comments\n")
            page = 1
            while True:
                comments = await self._client.get_json(
                    f"/repos/{repo}/issues/{number}/comments",
                    params={"per_page": "100", "page": str(page)},
                )
                if not comments:
                    break
                for c in comments:
                    lines.append(
                        f"### @{c.get('user', {}).get('login', 'unknown')} ({c.get('created_at', '')})"
                    )
                    lines.append(c.get("body", "") or "(empty)")
                    lines.append("")
                if len(comments) < 100:
                    break
                page += 1

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=f"{repo}/issues/{number}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"issue-{number}.md"},
        )

    async def _fetch_pr(self, repo: str, number: int) -> RawDocument:
        """Fetch pull request with description and review comments."""
        assert self._client is not None
        pr = await self._client.get_json(f"/repos/{repo}/pulls/{number}")

        lines = [
            f"# PR #{number}: {pr['title']}",
            f"**State:** {pr.get('state', 'unknown')}  ",
            f"**Author:** @{pr.get('user', {}).get('login', 'unknown')}  ",
            f"**Base:** {pr.get('base', {}).get('ref', '')} <- {pr.get('head', {}).get('ref', '')}  ",
            f"**Created:** {pr.get('created_at', '')}  ",
            "",
            pr.get("body") or "(no description)",
        ]

        # Fetch review comments (paginated — PRs can have >100 review comments)
        try:
            lines.append("\n---\n## Review Comments\n")
            page = 1
            has_reviews = False
            while True:
                reviews = await self._client.get_json(
                    f"/repos/{repo}/pulls/{number}/comments",
                    params={"per_page": "100", "page": str(page)},
                )
                if not reviews:
                    break
                has_reviews = True
                for r in reviews:
                    lines.append(
                        f"### @{r.get('user', {}).get('login', '')} on `{r.get('path', '')}`"
                    )
                    lines.append(r.get("body", "") or "(empty)")
                    lines.append("")
                if len(reviews) < 100:
                    break
                page += 1
            if not has_reviews:
                lines.pop()  # Remove the "Review Comments" header if none found
        except Exception:
            pass  # Non-critical

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=f"{repo}/pulls/{number}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"pr-{number}.md"},
        )

    async def _fetch_file(self, repo: str, ref: str, path: str) -> RawDocument:
        """Fetch a file's content from the repository."""
        assert self._client is not None
        data = await self._client.get_json(
            f"/repos/{repo}/contents/{path}",
            params={"ref": ref},
        )

        if data.get("encoding") == "base64":
            content = base64.b64decode(data["content"])
        else:
            content = (data.get("content", "") or "").encode("utf-8")

        return RawDocument(
            external_id=f"{repo}/blob/{ref}/{path}",
            content=content,
            content_type=_guess_content_type(path),
            metadata={"filename": path.split("/")[-1], "sha": data.get("sha", "")},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get permissions based on repository collaborator access."""
        assert self._client is not None
        parts = doc_id.split("/")
        repo = f"{parts[0]}/{parts[1]}"
        return await self._get_repo_permissions(repo)

    async def _get_repo_permissions(self, repo: str) -> list[PermissionEntry]:
        """Get all collaborator permissions for a repository."""
        assert self._client is not None
        entries: list[PermissionEntry] = []

        try:
            page = 1
            while True:
                collabs = await self._client.get_json(
                    f"/repos/{repo}/collaborators",
                    params={"per_page": "100", "page": str(page)},
                )
                if not collabs:
                    break

                for c in collabs:
                    perms = c.get("permissions", {})
                    # Pick the highest permission level
                    if perms.get("admin"):
                        role = "admin"
                    elif perms.get("maintain"):
                        role = "maintain"
                    elif perms.get("push"):
                        role = "push"
                    elif perms.get("triage"):
                        role = "triage"
                    else:
                        role = "pull"

                    entries.append(
                        PermissionEntry(
                            subject_type="user",
                            subject_id=c.get("login", ""),
                            relation=ROLE_MAP.get(role, "viewer"),
                        )
                    )

                if len(collabs) < 100:
                    break
                page += 1
        except Exception as e:
            logger.warning("Failed to get collaborators for %s: %s", repo, e)

        # Also get team permissions if this is an org repo
        try:
            teams = await self._client.get_json(
                f"/repos/{repo}/teams",
                params={"per_page": "100"},
            )
            for team in teams:
                team_perm = team.get("permission", "pull")
                entries.append(
                    PermissionEntry(
                        subject_type="group",
                        subject_id=f"{team.get('slug', '')}",
                        relation=ROLE_MAP.get(team_perm, "viewer"),
                    )
                )
        except Exception:
            pass  # Not all repos have team access

        return entries

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/rate_limit")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime from GitHub API."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _parse_github_url(url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL like https://github.com/owner/repo."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2:
        # Strip .git suffix if present
        repo = parts[1].removesuffix(".git")
        return f"{parts[0]}/{repo}"
    return None


def _guess_content_type(path: str) -> str:
    """Guess content type from file extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    mapping = {
        "md": "text/markdown",
        "mdx": "text/markdown",
        "txt": "text/plain",
        "rst": "text/x-rst",
        "py": "text/x-python",
        "js": "text/javascript",
        "ts": "text/typescript",
        "java": "text/x-java",
        "go": "text/x-go",
        "rs": "text/x-rust",
        "rb": "text/x-ruby",
        "cs": "text/x-csharp",
        "yaml": "text/yaml",
        "yml": "text/yaml",
        "json": "application/json",
        "toml": "text/toml",
    }
    return mapping.get(ext, "text/plain")
