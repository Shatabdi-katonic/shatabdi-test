"""Linear connector.

API: Linear GraphQL API
Auth: Bearer api_key
Sync: Incremental (updatedAt filter) via GraphQL cursor pagination
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
    ConnectorAuthError,
    ConnectorBase,
    ConnectorRateLimitError,
    ConnectorTransientError,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://api.linear.app/graphql"

# Shared selection set for an issue node (kept identical across both list
# variants so downstream parsing in list_documents() doesn't branch).
_LIST_ISSUES_FIELDS = """
    nodes {
      id
      identifier
      title
      description
      updatedAt
      url
      assignee { name email }
      state { name }
      team { name key }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
"""

# Full-sync / first-run variant: NO updatedAt filter. The inline ingest path
# always calls list_documents() with since=None, so this is the common path.
# Sending a filter here previously produced `{ updatedAt: { gt: null } }`,
# which Linear rejects with HTTP 400.
_LIST_ISSUES_QUERY = f"""
query ListIssues($after: String) {{
  issues(first: 50, after: $after) {{{_LIST_ISSUES_FIELDS}
  }}
}}
"""

# Incremental variant: filter by updatedAt. The comparator field
# (DateComparator.gt) expects Linear's `DateTimeOrDuration` scalar — declaring
# the variable as `DateTime` is a type-mismatch GraphQL validation error (400)
# regardless of the value passed.
_LIST_ISSUES_SINCE_QUERY = f"""
query ListIssues($after: String, $since: DateTimeOrDuration) {{
  issues(
    filter: {{ updatedAt: {{ gt: $since }} }}
    first: 50
    after: $after
  ) {{{_LIST_ISSUES_FIELDS}
  }}
}}
"""

_FETCH_ISSUE_QUERY = """
query FetchIssue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    url
    state { name }
    priority
    assignee { name email }
    team { name }
    labels { nodes { name } }
    comments {
      nodes {
        body
        user { name }
        createdAt
      }
    }
  }
}
"""


class LinearConnector(ConnectorBase):
    """Native Linear connector using the GraphQL API.

    Config:
        team_keys: Optional list of team keys to filter issues.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept the AddKnowledgeModal form keys (linearTeam) alongside the
        # canonical key (team_keys) — same field-mapping fallback as Outline/
        # ClickUp. linearTeam is a single key; wrap it into the list form.
        self._team_keys: list[str] = config.get("team_keys") or (
            [config.get("linearTeam")] if config.get("linearTeam") else []
        )
        # Token entered in the modal's "API Key" field arrives in config as
        # `linearApiKey`; hold it so authenticate() can fall back to it.
        self._config_token: str = config.get("linearApiKey") or config.get("api_key") or ""
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        """Authenticate using a Linear token (personal API key or OAuth access token)."""
        # Accept every place the token can arrive: OAuth access_token,
        # api_key/api_token credentials, or the config form field
        # (linearApiKey). Previously only `credentials.api_key` was read, so an
        # OAuth-connected source (access_token) or a form-entered key failed
        # with "Linear requires 'api_key' credential".
        api_key = (
            credentials.get("api_key")
            or credentials.get("access_token")
            or credentials.get("api_token")
            or self._config_token
            or ""
        )
        if not api_key:
            raise ConnectorAuthError(
                "Linear requires an API key (personal API key or OAuth access token)",
                connector_type="linear",
            )

        headers = bearer_headers(api_key)
        self._client = RetryClient(base_url="", headers=headers, rate_limiter=self.rate_limiter)

        # Verify access
        try:
            result = await self._graphql("{ viewer { id name email } }")
            viewer = result.get("viewer", {})
            logger.info("Linear authenticated as %s", viewer.get("name", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(
                f"Linear authentication failed: {exc}",
                connector_type="linear",
            ) from exc

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List Linear issues, optionally filtered by updatedAt."""
        assert self._client is not None

        cursor: str | None = None
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.000Z") if since else None
        # Pick the filtered query only when we actually have a `since`; the
        # unfiltered query avoids sending `gt: null` (Linear → HTTP 400).
        query = _LIST_ISSUES_SINCE_QUERY if since_str else _LIST_ISSUES_QUERY

        while True:
            variables: dict = {}
            if cursor:
                variables["after"] = cursor
            if since_str:
                variables["since"] = since_str

            try:
                result = await self._graphql(query, variables)
            except Exception as exc:
                _raise_mapped(exc, "linear")
                raise

            issues_data = result.get("issues", {})
            nodes = issues_data.get("nodes", [])

            for issue in nodes:
                team = issue.get("team", {})
                if self._team_keys and team.get("key") not in self._team_keys:
                    continue

                assignee = issue.get("assignee") or {}
                yield DocumentMetadata(
                    external_id=issue["id"],
                    title=f"{issue.get('identifier', '')}: {issue.get('title', '')}",
                    url=issue.get("url"),
                    content_type="text/plain",
                    author=assignee.get("email"),
                    modified_at=_parse_ts(issue.get("updatedAt", "")),
                    folder_id=team.get("key"),
                    metadata={
                        "identifier": issue.get("identifier"),
                        "state": (issue.get("state") or {}).get("name"),
                        "team": team.get("name"),
                    },
                )

            page_info = issues_data.get("pageInfo", {})
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                cursor = page_info["endCursor"]
            else:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch a single Linear issue and serialize to markdown."""
        assert self._client is not None

        try:
            result = await self._graphql(_FETCH_ISSUE_QUERY, {"id": doc_id})
        except Exception as exc:
            _raise_mapped(exc, "linear")
            raise

        issue = result.get("issue", {})
        parts: list[str] = []

        identifier = issue.get("identifier", doc_id)
        parts.append(f"# {identifier}: {issue.get('title', '')}")
        parts.append("")

        state = (issue.get("state") or {}).get("name", "")
        assignee = (issue.get("assignee") or {}).get("name", "Unassigned")
        team = (issue.get("team") or {}).get("name", "")
        labels = [l.get("name", "") for l in (issue.get("labels") or {}).get("nodes", [])]

        parts.append(f"**State:** {state}")
        parts.append(f"**Assignee:** {assignee}")
        if team:
            parts.append(f"**Team:** {team}")
        if labels:
            parts.append(f"**Labels:** {', '.join(labels)}")
        parts.append("")

        if issue.get("description"):
            parts.append(issue["description"])
            parts.append("")

        comments = (issue.get("comments") or {}).get("nodes", [])
        if comments:
            parts.append("## Comments")
            for c in comments:
                author = (c.get("user") or {}).get("name", "Unknown")
                parts.append(f"\n**{author}:**\n{c.get('body', '')}")

        content = "\n".join(parts)
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={"title": issue.get("title", ""), "identifier": identifier},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Linear does not expose document-level permissions."""
        return []

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._graphql("{ viewer { id } }")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query against the Linear API."""
        assert self._client is not None
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = await self._client.post(_GRAPHQL_URL, json=payload)
        body = resp.json()

        if "errors" in body:
            errors = body["errors"]
            msg = errors[0].get("message", str(errors)) if errors else "Unknown GraphQL error"
            logger.error("Linear GraphQL error: %s", msg)
            raise RuntimeError(f"Linear GraphQL error: {msg}")

        return body.get("data", {})


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
            retry_after = float(exc.response.headers.get("Retry-After", "5"))
            raise ConnectorRateLimitError(
                str(exc), connector_type=connector_type, retry_after=retry_after
            ) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
