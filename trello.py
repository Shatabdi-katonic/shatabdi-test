"""Trello connector.

API: Trello REST API v1
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (dateLastActivity filter)
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
_BASE = "https://api.trello.com/1"


class TrelloConnector(ConnectorBase):
    """Native Trello connector via REST API."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept the AddKnowledgeModal form keys (trelloBoards) alongside the
        # canonical key (board_ids). trelloBoards is a comma-separated string.
        self._board_ids: list[str] = config.get("board_ids") or [
            b.strip() for b in str(config.get("trelloBoards") or "").split(",") if b.strip()
        ]
        # Trello auths with an API key + user token (NOT OAuth2 code/state — its
        # authorize flow returns the token in the URL fragment, which never
        # reaches the server). Capture the modal's api-key form fields so an
        # api_key-style source works. Same field-mapping fallback as Outline/
        # ClickUp/Linear.
        self._config_key: str = config.get("trelloApiKey") or config.get("api_key") or config.get("key") or ""
        self._config_token: str = config.get("trelloToken") or config.get("token") or ""
        self._client: RetryClient | None = None
        self._token: str = ""
        self._key: str = ""

    async def authenticate(self, credentials: dict) -> None:
        token = (
            credentials.get("access_token")
            or credentials.get("token")
            or self._config_token
            or ""
        )
        api_key = (
            credentials.get("api_key")
            or credentials.get("key")
            or self._config_key
            or ""
        )
        if not token:
            raise ConnectorAuthError(
                "Trello requires an API token (generate at trello.com with your API key)",
                connector_type="trello",
            )
        self._token = token
        self._key = api_key
        headers = {"Authorization": f'OAuth oauth_consumer_key="{api_key}", oauth_token="{token}"'} if api_key else bearer_headers(token)
        self._client = RetryClient(base_url=_BASE, headers=headers)
        try:
            resp = await self._client.get("/members/me", params=self._auth_params())
            data = resp.json()
            logger.info("Trello authenticated as %s", data.get("fullName", "?"))
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Trello auth failed: {exc}", connector_type="trello") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None
        boards = self._board_ids
        if not boards:
            resp = await self._client.get("/members/me/boards", params={**self._auth_params(), "fields": "id"})
            boards = [b["id"] for b in resp.json()]
        for board_id in boards:
            params = {**self._auth_params(), "fields": "id,name,dateLastActivity,url,idBoard,desc", "limit": "1000"}
            if since:
                params["since"] = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            try:
                resp = await self._client.get(f"/boards/{board_id}/cards", params=params)
            except Exception as exc:
                _raise_mapped(exc, "trello")
                raise
            for card in resp.json():
                yield DocumentMetadata(
                    external_id=card["id"],
                    title=card.get("name", ""),
                    url=card.get("url"),
                    content_type="text/plain",
                    modified_at=_parse_ts(card.get("dateLastActivity", "")),
                    folder_id=board_id,
                    metadata={"board_id": board_id},
                )

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/cards/{doc_id}", params={**self._auth_params(), "actions": "commentCard", "actions_limit": "50", "checklists": "all"})
        except Exception as exc:
            _raise_mapped(exc, "trello")
            raise
        card = resp.json()
        parts = [f"# {card.get('name', doc_id)}", ""]
        if card.get("desc"):
            parts.append(card["desc"])
            parts.append("")
        for cl in card.get("checklists", []):
            parts.append(f"## Checklist: {cl.get('name', '')}")
            for item in cl.get("checkItems", []):
                check = "x" if item.get("state") == "complete" else " "
                parts.append(f"- [{check}] {item.get('name', '')}")
            parts.append("")
        actions = card.get("actions", [])
        if actions:
            parts.append("## Comments")
            for a in actions:
                author = (a.get("memberCreator") or {}).get("fullName", "Unknown")
                text = (a.get("data") or {}).get("text", "")
                parts.append(f"\n**{author}:**\n{text}")
        content = "\n".join(parts)
        return RawDocument(external_id=doc_id, content=content.encode(), content_type="text/plain", metadata={"title": card.get("name", "")})

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        return []

    async def health_check(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.get("/members/me", params=self._auth_params())
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    def _auth_params(self) -> dict:
        params = {}
        if self._key:
            params["key"] = self._key
            params["token"] = self._token
        return params


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
