"""SurveyMonkey connector.

API: SurveyMonkey REST API v3
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (date_modified sort)
Permissions: Not supported
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers
from platform_knowledge_engine.connectors.base import (
    ConnectorAuthError, ConnectorBase, ConnectorRateLimitError,
    ConnectorTransientError, DocumentMetadata, PermissionEntry, RawDocument,
)

logger = logging.getLogger(__name__)
_BASE = "https://api.surveymonkey.com/v3"


class SurveyMonkeyConnector(ConnectorBase):
    """Native SurveyMonkey connector via REST API v3."""

    def __init__(self, config: dict | None = None) -> None:
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("SurveyMonkey requires 'access_token'", connector_type="surveymonkey")
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))
        try:
            resp = await self._client.get("/users/me")
            me = resp.json()
            logger.info("SurveyMonkey authenticated as %s", me.get("username", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"SurveyMonkey auth failed: {exc}", connector_type="surveymonkey") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        page = 1
        while True:
            params: dict = {"page": page, "per_page": 100, "sort_by": "date_modified", "sort_order": "DESC"}
            if since:
                params["start_modified_at"] = since.strftime("%Y-%m-%dT%H:%M:%S")
            try:
                resp = await self._client.get("/surveys", params=params)
            except Exception as exc:
                _raise_mapped(exc, "surveymonkey")
                raise
            body = resp.json()
            for survey in body.get("data", []):
                modified = _parse_ts(survey.get("date_modified", ""))
                yield DocumentMetadata(
                    external_id=survey["id"],
                    title=survey.get("title", ""),
                    url=survey.get("href"),
                    content_type="text/plain",
                    modified_at=modified,
                    metadata={},
                )
            links = body.get("links", {})
            if not links.get("next"):
                break
            page += 1

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/surveys/{doc_id}/details")
        except Exception as exc:
            _raise_mapped(exc, "surveymonkey")
            raise
        survey = resp.json()
        parts = [f"# {survey.get('title', doc_id)}", ""]
        parts.append(f"**Response Count:** {survey.get('response_count', 0)}")
        parts.append(f"**Date Created:** {survey.get('date_created', '')}")
        parts.append("")
        for page_data in survey.get("pages", []):
            page_title = page_data.get("title") or page_data.get("description", "")
            if page_title:
                parts.append(f"## {page_title}")
            for question in page_data.get("questions", []):
                q_text = (question.get("headings") or [{}])[0].get("heading", "") if question.get("headings") else ""
                parts.append(f"\n**Q:** {q_text}")
                answers = question.get("answers", {})
                choices = answers.get("choices", [])
                for choice in choices:
                    parts.append(f"  - {choice.get('text', '')}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": survey.get("title", "")})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/users/me")
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
            raise ConnectorRateLimitError(str(exc), connector_type=connector_type, retry_after=float(exc.response.headers.get("Retry-After", "5"))) from exc
        if code >= 500:
            raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
    if isinstance(exc, httpx.TimeoutException):
        raise ConnectorTransientError(str(exc), connector_type=connector_type) from exc
