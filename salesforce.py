"""Salesforce connector.

API: Salesforce REST API (SOQL queries + Composite API)
Auth: OAuth 2.0 (Connected App) or username/password + security token
Sync: Incremental (LastModifiedDate) + full (bulk query)
Permissions: Salesforce sharing model (OWD, sharing rules, role hierarchy)

Content types indexed:
  - Knowledge articles (KnowledgeArticleVersion)
  - Cases (with comments)
  - Files (ContentDocument + ContentVersion)
  - Custom objects (configurable SOQL)

Role mapping (spec section 15):
  Owner/Admin   -> owner
  ReadWrite     -> editor
  Read          -> viewer
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
    ConnectorTransientError,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)

ROLE_MAP = {"Owner": "owner", "ReadWrite": "editor", "Read": "viewer"}


class SalesforceConnector(ConnectorBase):
    """Salesforce connector for knowledge articles, cases, and files."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Frontend field: sfInstanceUrl as fallback for instance_url
        self._instance_url: str = (
            config.get("instance_url", "") or config.get("sfInstanceUrl", "")
        ).rstrip("/")
        self._index_knowledge: bool = config.get("index_knowledge_articles", True)
        self._index_cases: bool = config.get("index_cases", False)
        self._index_files: bool = config.get("index_files", True)
        self._custom_soql: list[dict] = config.get("custom_queries", [])
        self._api_version: str = config.get("api_version", "v59.0")
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        instance_url = credentials.get("instance_url", "") or self._instance_url

        # Support OAuth client-credentials flow via sfClientId / sfClientSecret
        if not token:
            client_id = credentials.get("sfClientId", "")
            client_secret = credentials.get("sfClientSecret", "")
            if client_id and client_secret and instance_url:
                token, instance_url = await self._exchange_client_credentials(
                    instance_url, client_id, client_secret
                )

        if not token:
            raise ConnectorAuthError(
                "Salesforce connector requires access_token or sfClientId/sfClientSecret"
            )
        if not instance_url:
            raise ConnectorAuthError("Salesforce connector requires instance_url or sfInstanceUrl")

        self._instance_url = instance_url.rstrip("/")
        self._client = RetryClient(
            base_url=f"{self._instance_url}/services/data/{self._api_version}",
            headers=bearer_headers(token),
        )

        # Verify connectivity
        await self._client.get_json("/limits")
        logger.info("Salesforce authenticated at %s", self._instance_url)

    async def _exchange_client_credentials(
        self, instance_url: str, client_id: str, client_secret: str
    ) -> tuple[str, str]:
        """Exchange client credentials for an access token."""
        import httpx

        token_url = f"{instance_url.rstrip('/')}/services/oauth2/token"
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            if resp.status_code != 200:
                raise ConnectorAuthError(
                    f"Salesforce OAuth token exchange failed: {resp.text}"
                )
            data = resp.json()
        return data["access_token"], data.get("instance_url", instance_url)

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        since_clause = ""
        if since:
            since_clause = f" WHERE LastModifiedDate > {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"

        # Knowledge articles. KnowledgeArticleVersion only exists when Salesforce
        # Knowledge is enabled, and Salesforce REQUIRES a PublishStatus filter on
        # it — querying it without the filter (or in an org without Knowledge)
        # returns 400. Each object query is wrapped so one unavailable/erroring
        # object (e.g. Knowledge not enabled in a dev-ed org) doesn't abort the
        # discovery of Cases and Files. (CR-576)
        if self._index_knowledge:
            knowledge_where = "WHERE PublishStatus='Online'"
            if since:
                knowledge_where += (
                    f" AND LastModifiedDate > {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                )
            soql = (
                "SELECT Id, Title, UrlName, ArticleNumber, LastModifiedDate, "
                "CreatedById, Summary "
                f"FROM KnowledgeArticleVersion {knowledge_where} "
                "ORDER BY LastModifiedDate DESC"
            )
            try:
                async for record in self._query(soql):
                    yield DocumentMetadata(
                        external_id=f"knowledge:{record['Id']}",
                        title=record.get("Title", "Untitled Article"),
                        url=f"{self._instance_url}/lightning/r/KnowledgeArticleVersion/{record['Id']}/view",
                        content_type="text/html",
                        author=record.get("CreatedById"),
                        modified_at=_parse_sf_dt(record.get("LastModifiedDate", "")),
                        metadata={
                            "type": "knowledge_article",
                            "article_number": record.get("ArticleNumber"),
                        },
                    )
            except Exception as e:
                logger.warning(
                    "Salesforce Knowledge articles skipped (KnowledgeArticleVersion "
                    "unavailable — Knowledge not enabled or query unsupported): %s", e
                )

        # Cases
        if self._index_cases:
            soql = (
                "SELECT Id, CaseNumber, Subject, Description, Status, "
                "LastModifiedDate, OwnerId "
                f"FROM Case{since_clause} "
                "ORDER BY LastModifiedDate DESC LIMIT 10000"
            )
            try:
                async for record in self._query(soql):
                    yield DocumentMetadata(
                        external_id=f"case:{record['Id']}",
                        title=f"[Case {record.get('CaseNumber', '')}] {record.get('Subject', '')}",
                        url=f"{self._instance_url}/lightning/r/Case/{record['Id']}/view",
                        content_type="text/plain",
                        author=record.get("OwnerId"),
                        modified_at=_parse_sf_dt(record.get("LastModifiedDate", "")),
                        metadata={"type": "case", "status": record.get("Status")},
                    )
            except Exception as e:
                logger.warning("Salesforce Cases skipped (object unavailable or query failed): %s", e)

        # Files (ContentDocument)
        if self._index_files:
            soql = (
                "SELECT Id, Title, FileType, ContentSize, OwnerId, "
                "LastModifiedDate, LatestPublishedVersionId "
                f"FROM ContentDocument{since_clause} "
                "ORDER BY LastModifiedDate DESC LIMIT 10000"
            )
            try:
                async for record in self._query(soql):
                    yield DocumentMetadata(
                        external_id=f"file:{record['Id']}",
                        title=record.get("Title", "Untitled"),
                        url=f"{self._instance_url}/lightning/r/ContentDocument/{record['Id']}/view",
                        content_type=_sf_file_type(record.get("FileType", "")),
                        size_bytes=record.get("ContentSize"),
                        author=record.get("OwnerId"),
                        modified_at=_parse_sf_dt(record.get("LastModifiedDate", "")),
                        metadata={
                            "type": "file",
                            "file_type": record.get("FileType"),
                            "version_id": record.get("LatestPublishedVersionId"),
                        },
                    )
            except Exception as e:
                logger.warning("Salesforce Files skipped (object unavailable or query failed): %s", e)

    async def _query(self, soql: str) -> AsyncIterator[dict]:
        """Execute SOQL query with pagination (nextRecordsUrl)."""
        assert self._client is not None
        from urllib.parse import quote
        url = f"/query?q={quote(soql, safe='')}"

        while url:
            try:
                data = await self._client.get_json(url)
            except Exception as e:
                logger.error("Salesforce SOQL query failed: %s", e)
                raise
            for record in data.get("records", []):
                yield record
            next_url = data.get("nextRecordsUrl")
            url = next_url if next_url else ""

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        doc_type, sf_id = doc_id.split(":", 1)

        if doc_type == "knowledge":
            return await self._fetch_knowledge(sf_id)
        elif doc_type == "case":
            return await self._fetch_case(sf_id)
        elif doc_type == "file":
            return await self._fetch_file(sf_id)
        else:
            raise ValueError(f"Unknown Salesforce doc type: {doc_type}")

    async def _fetch_knowledge(self, sf_id: str) -> RawDocument:
        assert self._client is not None
        article = await self._client.get_json(f"/sobjects/KnowledgeArticleVersion/{sf_id}")
        # Knowledge articles may have rich text fields
        title = article.get("Title", "Untitled")
        body = article.get("ArticleBody", article.get("Summary", ""))
        content = f"# {title}\n\n{body}".encode()
        return RawDocument(
            external_id=f"knowledge:{sf_id}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"article-{sf_id}.md"},
        )

    async def _fetch_case(self, sf_id: str) -> RawDocument:
        assert self._client is not None
        case = await self._client.get_json(f"/sobjects/Case/{sf_id}")

        lines = [
            f"# Case {case.get('CaseNumber', '')}: {case.get('Subject', '')}",
            f"**Status:** {case.get('Status', '')}",
            f"**Priority:** {case.get('Priority', '')}",
            f"**Created:** {case.get('CreatedDate', '')}",
            "",
            case.get("Description") or "(no description)",
        ]

        # Fetch case comments
        try:
            comments_data = await self._client.get_json(
                f"/query?q=SELECT Id,CommentBody,CreatedDate,CreatedById "
                f"FROM CaseComment WHERE ParentId='{sf_id.replace(chr(39), "")}' ORDER BY CreatedDate"
            )
            if comments_data.get("records"):
                lines.append("\n---\n## Comments\n")
                for c in comments_data["records"]:
                    lines.append(f"### {c.get('CreatedDate', '')}")
                    lines.append(c.get("CommentBody", "") or "(empty)")
                    lines.append("")
        except Exception:
            pass

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=f"case:{sf_id}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"case-{sf_id}.md"},
        )

    async def _fetch_file(self, sf_id: str) -> RawDocument:
        assert self._client is not None
        # Get latest version info
        doc = await self._client.get_json(f"/sobjects/ContentDocument/{sf_id}")
        version_id = doc.get("LatestPublishedVersionId", "")

        # Download binary content
        resp = await self._client.get(f"/sobjects/ContentVersion/{version_id}/VersionData")
        content_type = _sf_file_type(doc.get("FileType", ""))
        return RawDocument(
            external_id=f"file:{sf_id}",
            content=resp.content,
            content_type=content_type,
            metadata={"filename": doc.get("Title", sf_id)},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get sharing permissions for a Salesforce record.

        Uses the record's sharing table (e.g., CaseShare, ContentDocumentLink).
        """
        assert self._client is not None
        doc_type, sf_id = doc_id.split(":", 1)
        entries: list[PermissionEntry] = []

        try:
            if doc_type == "file":
                # ContentDocumentLink tracks who has access to files
                data = await self._client.get_json(
                    f"/query?q=SELECT LinkedEntityId,ShareType "
                    f"FROM ContentDocumentLink WHERE ContentDocumentId='{sf_id.replace(chr(39), "")}'"
                )
                for link in data.get("records", []):
                    share_type = link.get("ShareType", "V")
                    relation = "viewer" if share_type in ("V", "I") else "editor"
                    entries.append(
                        PermissionEntry(
                            subject_type="user",
                            subject_id=link.get("LinkedEntityId", ""),
                            relation=relation,
                        )
                    )
            else:
                # For other objects, check the Share table
                obj_type = "Case" if doc_type == "case" else "KnowledgeArticleVersion"
                try:
                    data = await self._client.get_json(
                        f"/query?q=SELECT UserOrGroupId,AccessLevel "
                        f"FROM {obj_type}Share WHERE ParentId='{sf_id.replace(chr(39), "")}'"
                    )
                    for share in data.get("records", []):
                        level = share.get("AccessLevel", "Read")
                        entries.append(
                            PermissionEntry(
                                subject_type="user",
                                subject_id=share.get("UserOrGroupId", ""),
                                relation=ROLE_MAP.get(level, "viewer"),
                            )
                        )
                except Exception:
                    pass  # Not all objects have Share tables
        except Exception as e:
            logger.warning("Failed to get permissions for %s: %s", doc_id, e)

        return entries

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/limits")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _parse_sf_dt(s: str) -> datetime:
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _sf_file_type(ft: str) -> str:
    mapping = {
        "PDF": "application/pdf",
        "WORD_X": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "EXCEL_X": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "POWER_POINT_X": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "PNG": "image/png",
        "JPG": "image/jpeg",
        "CSV": "text/csv",
        "TEXT": "text/plain",
    }
    return mapping.get(ft.upper(), "application/octet-stream")
