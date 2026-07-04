"""Jira connector.

API: Jira REST API v3 (Cloud) or v2 (Server/DC)
Auth: OAuth 2.0 (Cloud) or API token (Server)
Sync: Incremental (JQL updatedDate filter) + full
Permissions: Project roles -> folder-level permissions

Role mapping (spec section 15.5 / Jira):
  Project admin     -> editor
  Developer         -> editor
  Viewer            -> viewer
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

# The enhanced /rest/api/3/search/jql endpoint returns 400 for UNBOUNDED JQL
# (queries with no narrowing predicate). This bounded probe matches every issue
# — every Jira issue has a created date after the epoch — while satisfying the
# endpoint's bounded-query requirement.
_BOUNDED_PROBE_JQL = 'created >= "1970-01-01" order by created DESC'

ROLE_MAP = {
    "administrators": "editor",
    "atlassian-addons-project-access": "viewer",
    "developers": "editor",
    "member": "editor",
    "viewer": "viewer",
    "servicedesk-team": "editor",
}


class JiraConnector(ConnectorBase):
    """Native Jira connector.

    Config:
        base_url: Jira instance URL (e.g., https://acme.atlassian.net)
        project_keys: Specific projects to sync. Empty = all accessible.
        is_cloud: True for Atlassian Cloud. Default True.
    """

    CONFIG_SCHEMA = [
        ConfigField(
            key="jiraProject",
            label="Project key filter",
            type="text",
            required=False,
            placeholder="e.g. ENG",
            help_text="Single project key to restrict the sync. Leave blank to sync all projects accessible to the credential.",
        ),
        ConfigField(
            key="is_cloud",
            label="Atlassian Cloud",
            type="boolean",
            required=False,
            default=True,
            help_text="Disable only for self-hosted Jira Server / Data Center instances.",
        ),
    ]

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Support both canonical and frontend field names
        self._base_url: str = (
            config.get("base_url") or config.get("jiraUrl") or ""
        ).rstrip("/")
        project_keys = config.get("project_keys") or []
        if not project_keys:
            # Frontend sends a single project key as "jiraProject"
            jp = config.get("jiraProject", "")
            if jp:
                project_keys = [jp]
        self._project_keys: list[str] = project_keys
        self._is_cloud: bool = config.get("is_cloud", True)
        self._config = config
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        headers: dict[str, str] = {}

        # Merge config-level credentials (frontend embeds them in config)
        merged = {**credentials}
        if "jiraToken" in self._config and "api_token" not in merged:
            merged["api_token"] = self._config["jiraToken"]
        if "jiraEmail" in self._config and "email" not in merged:
            merged["email"] = self._config["jiraEmail"]

        is_oauth = "access_token" in merged
        if is_oauth:
            headers = bearer_headers(merged["access_token"])
        elif "api_token" in merged and "email" in merged:
            import base64

            cred_str = f"{merged['email']}:{merged['api_token']}"
            b64 = base64.b64encode(cred_str.encode()).decode()
            headers = {"Authorization": f"Basic {b64}"}
        else:
            raise ConnectorAuthError(
                "Jira requires access_token or api_token+email",
                connector_type="jira",
            )

        # Atlassian Cloud OAuth 3LO (audience=api.atlassian.com) issues Bearer
        # tokens that must route through api.atlassian.com/ex/jira/{cloudId}/
        # — NOT the tenant URL. Using the tenant URL with a Bearer token often
        # returns 200 on /myself via legacy paths but returns 0 issues on
        # /search because the token isn't bound to that tenant directly.
        # Resolve cloudId via /oauth/token/accessible-resources when the token
        # is OAuth (basic auth with tenant URL works as-is).
        if is_oauth and self._is_cloud:
            cloud_id = await self._resolve_cloud_id(headers)
            if cloud_id:
                api_base = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
                logger.info("jira_cloud_oauth routing via cloudId=%s", cloud_id)
            elif self._base_url:
                api_base = f"{self._base_url}/rest/api/3"
            else:
                raise ConnectorAuthError(
                    "Jira Cloud OAuth: failed to resolve cloudId and no base_url configured",
                    connector_type="jira",
                )
        else:
            api_base = (
                f"{self._base_url}/rest/api/3" if self._is_cloud else f"{self._base_url}/rest/api/2"
            )
        self._client = RetryClient(base_url=api_base, headers=headers)

        try:
            # Verify with the Enhanced JQL search endpoint. Atlassian removed
            # the classic GET /rest/api/3/search (sunset 2025) and it is scoped
            # to the classic read:jira-work permission, which granular OAuth
            # tokens (read:issue:jira, read:jql:jira, ...) do NOT carry — so the
            # 3LO gateway returns 401 Unauthorized. The replacement
            # /rest/api/3/search/jql is authorized by the granular
            # read:jql:jira + read:issue:jira scopes the token actually has, and
            # also confirms the token is bound to this cloudId.
            # NOTE: /search/jql rejects UNBOUNDED queries (no narrowing
            # predicate) with 400 Bad Request — unlike the old /search. A bare
            # "order by created DESC" is unbounded, so we add a trivially-true
            # created bound that still matches every issue.
            await self._client.get_json(
                "/search/jql",
                params={
                    "jql": _BOUNDED_PROBE_JQL,
                    "maxResults": 1,
                    "fields": "key",
                },
            )
        except Exception as e:
            raise ConnectorAuthError(
                f"Jira authentication verification failed: {e}",
                connector_type="jira",
            ) from e
        logger.info("Jira authenticated, api_base=%s", api_base)

    async def _resolve_cloud_id(self, headers: dict[str, str]) -> str:
        """Look up the Atlassian Cloud cloudId for the current OAuth token."""
        probe = RetryClient(
            base_url="https://api.atlassian.com",
            headers=headers,
        )
        try:
            resources = await probe.get_json("/oauth/token/accessible-resources")
            if not isinstance(resources, list) or not resources:
                logger.warning("jira_accessible_resources empty — token has no Jira tenants")
                return ""
            # CR-619: choose the tenant in priority order:
            #   1. the configured base_url, if set;
            #   2. a tenant whose scopes mention Jira (so we don't route Jira API
            #      calls at a Confluence-only site → 401 on /ex/jira/{cloudId});
            #   3. the first tenant (fallback).
            # The old code blindly took resources[0], which 401'd when the first
            # accessible site wasn't Jira-capable.
            chosen = None
            if self._base_url:
                chosen = next(
                    (r for r in resources if r.get("url", "").rstrip("/") == self._base_url),
                    None,
                )
            if chosen is None:
                chosen = next(
                    (r for r in resources if any("jira" in s for s in (r.get("scopes") or []))),
                    None,
                )
            if chosen is None:
                logger.warning(
                    "jira_no_jira_capable_tenant — none of %d accessible site(s) have Jira "
                    "scopes; the token/site may not have Jira provisioned. Sites: %s",
                    len(resources),
                    [r.get("url") for r in resources],
                )
                chosen = resources[0]
            cloud_id = chosen.get("id", "")
            logger.info(
                "jira_cloud_id_resolved cloud_id=%s url=%s scopes=%s",
                cloud_id,
                chosen.get("url"),
                chosen.get("scopes"),
            )
            if not self._base_url and chosen.get("url"):
                self._base_url = chosen["url"].rstrip("/")
            return cloud_id
        except Exception as exc:
            logger.warning("jira_resolve_cloud_id_failed: %s", exc)
            return ""
        finally:
            await probe.close()

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """Each Jira issue becomes a document."""
        assert self._client is not None

        jql_parts: list[str] = []
        if self._project_keys:
            keys = ", ".join(f'"{k}"' for k in self._project_keys)
            jql_parts.append(f"project in ({keys})")
        if since:
            jql_parts.append(f'updated >= "{since.strftime("%Y-%m-%d %H:%M")}"')

        # /search/jql rejects unbounded queries (400). When neither a project
        # filter nor an incremental `since` bound is set, add a trivially-true
        # created bound so the query is considered bounded but still matches all.
        if not jql_parts:
            jql_parts.append('created >= "1970-01-01"')
        jql = " AND ".join(jql_parts) + " ORDER BY updated DESC"
        next_page_token: str | None = None

        try:
            while True:
                # Enhanced JQL search (replaces the removed GET /rest/api/3/
                # search). Pagination is cursor-based via nextPageToken — there
                # is no startAt/total. Stop when the response is the last page
                # (isLast) or no nextPageToken is returned.
                params = {
                    "jql": jql,
                    "maxResults": "100",
                    "fields": "summary,updated,creator,project,issuetype,status,description",
                }
                if next_page_token:
                    params["nextPageToken"] = next_page_token

                data = await self._client.get_json("/search/jql", params=params)

                issues = data.get("issues", [])
                if not issues:
                    break

                for issue in issues:
                    fields = issue.get("fields", {})
                    project = fields.get("project", {})

                    yield DocumentMetadata(
                        external_id=issue["key"],
                        title=f"{issue['key']}: {fields.get('summary', '')}",
                        url=f"{self._base_url}/browse/{issue['key']}",
                        content_type="text/plain",
                        author=fields.get("creator", {}).get("emailAddress"),
                        modified_at=_parse_timestamp(fields.get("updated", "")),
                        folder_id=project.get("key"),
                        metadata={
                            "project_key": project.get("key"),
                            "issue_type": fields.get("issuetype", {}).get("name"),
                            "status": fields.get("status", {}).get("name"),
                        },
                    )

                next_page_token = data.get("nextPageToken")
                if data.get("isLast") or not next_page_token:
                    break
        except ConnectorTransientError:
            raise
        except Exception as e:
            logger.error("Error listing Jira issues: %s", e)
            raise ConnectorTransientError(
                f"Error listing Jira issues: {e}",
                connector_type="jira",
            ) from e

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch issue as structured text (summary + description + comments).

        Fetches via the enhanced JQL search endpoint (key = "<doc_id>") rather
        than GET /rest/api/3/issue/{key}. On the OAuth 2.0 (3LO) gateway the
        Get-issue endpoint returns 401 "scope does not match" even with the full
        granular read:jira-work scope set, whereas /search/jql honours the
        granular scopes (same reason /search had to move to /search/jql).
        Querying by key returns exactly one issue with the requested fields.
        """
        assert self._client is not None

        safe_key = doc_id.replace('"', '\\"')
        result = await self._client.get_json(
            "/search/jql",
            params={
                "jql": f'key = "{safe_key}"',
                "maxResults": 1,
                "fields": "summary,description,comment",
            },
        )
        issues = result.get("issues", [])
        if not issues:
            raise ConnectorTransientError(
                f"Jira issue {doc_id} not returned by /search/jql",
                connector_type="jira",
            )
        fields = issues[0].get("fields", {})

        parts: list[str] = []
        parts.append(f"# {doc_id}: {fields.get('summary', '')}")
        parts.append("")

        # Description (ADF -> plain text)
        desc = fields.get("description")
        if desc:
            if isinstance(desc, dict):
                parts.append(_adf_to_text(desc))
            else:
                parts.append(str(desc))
            parts.append("")

        # Comments
        comments = fields.get("comment", {}).get("comments", [])
        if comments:
            parts.append("## Comments")
            for c in comments:
                author = c.get("author", {}).get("displayName", "Unknown")
                body = c.get("body")
                if isinstance(body, dict):
                    text = _adf_to_text(body)
                else:
                    text = str(body or "")
                parts.append(f"\n**{author}:**\n{text}")

        content = "\n".join(parts)
        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={"title": fields.get("summary", "")},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Check issue-level security restrictions.

        Jira issues normally inherit project permissions (handled by
        get_folder_permissions). But issues with a security level are
        restricted to specific users/groups beyond the project scope.
        When a security level is set, we return the issue reporter and
        assignee as explicit entries — project-level permissions are
        intersected by the pipeline.
        """
        assert self._client is not None
        entries: list[PermissionEntry] = []

        try:
            # Via /search/jql (see fetch_document) — GET /issue/{key} 401s on
            # the 3LO gateway even with full granular scopes.
            safe_key = doc_id.replace('"', '\\"')
            result = await self._client.get_json(
                "/search/jql",
                params={
                    "jql": f'key = "{safe_key}"',
                    "maxResults": 1,
                    "fields": "security,reporter,assignee",
                },
            )
            issues = result.get("issues", [])
            fields = issues[0].get("fields", {}) if issues else {}

            # If issue has a security level, it restricts visibility
            if fields.get("security"):
                logger.info("Jira issue %s has security level: %s", doc_id, fields["security"].get("name"))
                # Add reporter and assignee as explicit viewers
                reporter = fields.get("reporter", {})
                if reporter and reporter.get("emailAddress"):
                    entries.append(PermissionEntry("user", reporter["emailAddress"], "viewer"))
                assignee = fields.get("assignee", {})
                if assignee and assignee.get("emailAddress"):
                    entries.append(PermissionEntry("user", assignee["emailAddress"], "viewer"))
        except Exception as e:
            logger.debug("Could not check security level for %s: %s", doc_id, e)

        return entries

    async def get_folder_permissions(self, folder_id: str) -> list[PermissionEntry]:
        """Get project role members (folder_id = project key)."""
        assert self._client is not None
        entries: list[PermissionEntry] = []

        try:
            roles_data = await self._client.get_json(f"/project/{folder_id}/role")
            # roles_data is a dict of role_name -> role_url
            for role_name, role_url in roles_data.items():
                mapped = _map_role(role_name)
                # Fetch role members — role_url is absolute, extract the path
                from urllib.parse import urlparse
                try:
                    parsed = urlparse(role_url)
                    role_path = parsed.path
                    if parsed.query:
                        role_path += f"?{parsed.query}"
                    role_data = await self._client.get_json(role_path)
                except Exception:
                    # Fallback: try the full URL directly
                    try:
                        role_data = await self._client.get_json(role_url)
                    except Exception:
                        logger.debug("Could not fetch role %s for project %s", role_name, folder_id)
                        continue

                for actor in role_data.get("actors", []):
                    actor_type = actor.get("type", "")
                    if actor_type == "atlassian-user-role-actor":
                        email = actor.get("actorUser", {}).get("emailAddress", "")
                        if email:
                            entries.append(PermissionEntry("user", email, mapped))
                    elif actor_type == "atlassian-group-role-actor":
                        group_name = actor.get("displayName", actor.get("name", ""))
                        if group_name:
                            entries.append(PermissionEntry("group", group_name, mapped))
        except Exception as e:
            logger.warning("Failed to get project roles for %s: %s", folder_id, e)

        return entries

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            # Enhanced JQL search — bounded query (unbounded 400s on /search/jql).
            await self._client.get_json(
                "/search/jql",
                params={
                    "jql": _BOUNDED_PROBE_JQL,
                    "maxResults": 1,
                    "fields": "key",
                },
            )
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _adf_to_text(adf: dict) -> str:
    """Convert Atlassian Document Format to plain text (basic extraction)."""
    parts: list[str] = []

    def _walk(node: dict) -> None:
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        elif node.get("type") == "hardBreak":
            parts.append("\n")
        for child in node.get("content", []):
            _walk(child)
        if node.get("type") in ("paragraph", "heading", "blockquote", "listItem"):
            parts.append("\n")

    _walk(adf)
    return "".join(parts).strip()


def _map_role(role_name: str) -> str:
    key = role_name.lower().strip()
    return ROLE_MAP.get(key, "viewer")


def _parse_timestamp(ts: str) -> datetime:
    if not ts:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
