"""Miro connector.

API: Miro REST API v2
Auth: Bearer access_token (OAuth 2.0)
Sync: Incremental (modifiedAt sort)
Permissions: Not supported
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import RetryClient, bearer_headers
from platform_knowledge_engine.connectors.base import (
    ConfigField, ConnectorAuthError, ConnectorBase, ConnectorRateLimitError,
    ConnectorTransientError, DocumentMetadata, PermissionEntry, RawDocument,
)

logger = logging.getLogger(__name__)
_BASE = "https://api.miro.com/v2"


class MiroConnector(ConnectorBase):
    """Native Miro connector via REST API v2."""

    CONFIG_SCHEMA = [
        ConfigField(
            key="team_id",
            label="Team ID",
            type="text",
            required=False,
            placeholder="e.g. 3458764665467756639",
            help_text="Leave blank to auto-detect from the OAuth token. Override only if your grant covers multiple teams and you need to pick a specific one.",
        ),
    ]

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._team_id: str = config.get("team_id", "")
        self._client: RetryClient | None = None
        # Owner user_id captured from OAuth credentials at authenticate-time.
        # Used by ``get_permissions`` to write a SpiceDB ``owner`` relation for
        # every board this connector ingests — matching the file_upload pattern
        # (file_upload.py:162-172). Without this, ``get_permissions`` returned
        # an empty list, no SpiceDB relationships were written, and the
        # retriever's permission filter (retriever.py:626) excluded every Miro
        # chunk from search results — symptom: ``total_chunks > 0`` in Postgres
        # but zero hits in /search, even with threshold lowered to 0.
        self._owner_user_id: str = ""

    async def authenticate(self, credentials: dict) -> None:
        token = credentials.get("access_token", "")
        if not token:
            raise ConnectorAuthError("Miro requires 'access_token'", connector_type="miro")
        # Capture the platform user_id (Keycloak sub) so ``get_permissions``
        # can declare them as the document owner. Prefer
        # ``platform_user_id`` (the canonical Keycloak UUID injected by the
        # OAuth callback in oauth.py) over ``user_id`` (which is Miro's
        # provider-native numeric user id from the OAuth token response —
        # e.g. "3458764655068838822" — and doesn't match the Keycloak UUID
        # format the resolver expects, causing IdentityResolver to drop
        # the entry: see the original "1 connector entries -> 0 resolved"
        # log signature). ``user_id`` is the fallback only for credential
        # records saved before the oauth.py fix landed.
        self._owner_user_id = (
            credentials.get("platform_user_id")
            or credentials.get("user_id")
            or ""
        ).strip()
        # Miro returns team_id in the OAuth token response for single-team
        # installs. Use it when source config didn't specify one — required
        # for /v2/boards to return team-owned boards (without it, only boards
        # the user *directly* owns are returned).
        if not self._team_id:
            self._team_id = credentials.get("team_id", "") or ""
        logger.info(
            "miro_connector_init token_present=%s team_id=%r cred_keys=%s",
            bool(token),
            self._team_id,
            sorted(credentials.keys()),
        )
        self._client = RetryClient(base_url=_BASE, headers=bearer_headers(token))

        # Fallback: when team_id wasn't returned by OAuth (multi-team tokens,
        # Enterprise orgs, or older grants issued before we persisted extras),
        # introspect the token via /v1/oauth-token to get the definitive team.
        if not self._team_id:
            try:
                info_client = RetryClient(
                    base_url="https://api.miro.com/v1",
                    headers=bearer_headers(token),
                )
                try:
                    info_resp = await info_client.get("/oauth-token")
                    info = info_resp.json()
                    self._team_id = ((info.get("team") or {}).get("id") or "") or ""
                    logger.info(
                        "miro_oauth_introspect team_id=%r user=%r scopes=%s",
                        self._team_id,
                        (info.get("user") or {}).get("id", ""),
                        info.get("scopes") or info.get("scope"),
                    )
                finally:
                    await info_client.close()
            except Exception as exc:
                logger.warning("miro_oauth_introspect_failed: %s", exc)

        try:
            probe_params: dict = {"limit": 1}
            if self._team_id:
                probe_params["team_id"] = self._team_id
            resp = await self._client.get("/boards", params=probe_params)
            body = resp.json()
            logger.info(
                "miro_auth_probe status=%s total=%s size=%s team_id=%r keys=%s",
                resp.status_code,
                body.get("total"),
                body.get("size"),
                self._team_id,
                list(body.keys()),
            )
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise ConnectorAuthError(f"Miro auth failed: {exc}", connector_type="miro") from exc

    async def list_documents(self, since: datetime | None = None) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None

        # Strategy:
        #   1. If team_id is known, query /v2/boards?team_id=X.
        #   2. If that yields 0 boards, retry /v2/boards without team_id. This
        #      catches the case where the stored team_id is for a team the user
        #      isn't active in, or boards live in a different team under the
        #      same grant. Without team_id, Miro returns boards the user has
        #      direct access to across teams.
        attempts: list[dict] = []
        if self._team_id:
            attempts.append({"team_id": self._team_id})
        attempts.append({})  # fallback: no team filter

        seen_ids: set[str] = set()
        total_yielded = 0

        for attempt_idx, extra_params in enumerate(attempts):
            cursor: str | None = None
            page = 0
            attempt_yielded = 0
            while True:
                params: dict = {"limit": 50, "sort": "last_modified", **extra_params}
                if cursor:
                    params["cursor"] = cursor
                try:
                    resp = await self._client.get("/boards", params=params)
                except Exception as exc:
                    _raise_mapped(exc, "miro")
                    raise
                body = resp.json()
                page += 1
                data = body.get("data", []) or []
                logger.info(
                    "miro_list_boards attempt=%d page=%d status=%s total=%s size=%s data_len=%s params=%s",
                    attempt_idx,
                    page,
                    resp.status_code,
                    body.get("total"),
                    body.get("size"),
                    len(data),
                    params,
                )
                for board in data:
                    board_id = board.get("id")
                    if not board_id or board_id in seen_ids:
                        continue
                    seen_ids.add(board_id)
                    modified = _parse_ts(board.get("modifiedAt", ""))
                    if since and modified < since:
                        continue
                    attempt_yielded += 1
                    total_yielded += 1
                    yield DocumentMetadata(
                        external_id=board_id,
                        title=board.get("name", ""),
                        url=board.get("viewLink"),
                        content_type="text/plain",
                        modified_at=modified,
                        metadata={"description": (board.get("description") or "")[:200]},
                    )
                cursor = body.get("cursor")
                if not cursor or len(data) < 50:
                    break

            # If this attempt yielded anything, don't try the fallback — we have
            # authoritative results scoped to the configured team.
            if attempt_yielded > 0:
                break

        if total_yielded == 0:
            logger.warning(
                "miro_list_boards_empty team_id=%r attempts=%d — "
                "account has no accessible boards in team or token lacks access. "
                "Check: (1) the Miro user is a member of team %r, "
                "(2) at least one board exists there, "
                "(3) OAuth app scope includes boards:read.",
                self._team_id,
                len(attempts),
                self._team_id,
            )

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        try:
            resp = await self._client.get(f"/boards/{doc_id}")
        except Exception as exc:
            _raise_mapped(exc, "miro")
            raise
        board = resp.json()
        parts = [f"# {board.get('name', doc_id)}", ""]
        if board.get("description"):
            parts.append(_strip_html(board["description"]).strip())
            parts.append("")
        owner = (board.get("owner") or {}).get("name", "")
        if owner:
            parts.append(f"**Owner:** {owner}")
        # NOTE: the board's modifiedAt is intentionally NOT appended as a
        # standalone body paragraph. Doing so produced useless few-token
        # chunks containing only a timestamp (e.g. "2026 03 27T13:04:36Z")
        # that the chunker split off on their own. It lives in metadata
        # below instead, where the retriever can still use it.

        # Paginate through ALL board items (cursor-based) and capture the
        # FULL text of each — no 200-char truncation. The previous
        # implementation fetched only the first 50 items and clipped each
        # item to text[:200], so a board carrying a real document or long
        # text widgets was indexed as a handful of tokens. Symptom: the
        # board was visibly full of content in Miro, but /search "returned
        # nothing" because almost nothing was ever extracted/embedded.
        # Miro returns rich text as HTML in data.content (<h1>…</h1><p>…),
        # so strip tags to clean plain text before indexing.
        item_texts: list[str] = []
        cursor: str | None = None
        pages = 0
        try:
            while True:
                params: dict = {"limit": 50}
                if cursor:
                    params["cursor"] = cursor
                items_resp = await self._client.get(f"/boards/{doc_id}/items", params=params)
                body = items_resp.json()
                for item in body.get("data", []) or []:
                    item_type = item.get("type", "") or "item"
                    data = item.get("data", {}) or {}
                    raw = (
                        data.get("content")
                        or data.get("title")
                        or data.get("text")
                        or data.get("description")
                        or ""
                    )
                    text = _strip_html(raw).strip()
                    if text:
                        item_texts.append(f"### {item_type}\n{text}")
                cursor = (body.get("cursor") or "") or None
                pages += 1
                # Safety cap: 50 pages * 50 items = up to 2 500 items.
                if not cursor or pages >= 50:
                    break
        except Exception as exc:
            logger.warning("miro_fetch_items_failed board=%s pages=%d: %s", doc_id, pages, exc)

        if item_texts:
            parts.append("## Board Content")
            parts.append("")
            parts.extend(item_texts)

        content = "\n\n".join(p for p in parts if p)
        return RawDocument(
            external_id=doc_id,
            content=content.encode(),
            content_type="text/plain",
            metadata={
                "title": board.get("name", ""),
                "modified_at": board.get("modifiedAt", ""),
                "item_count": len(item_texts),
            },
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Miro's REST API does not expose per-board ACLs (no
        ``/boards/{id}/permissions`` endpoint). Treat every ingested board as
        owned by the user who completed the OAuth flow — same behaviour as
        ``file_upload`` (file_upload.py:162-172). This is what enables the
        SpiceDB relationship needed for the retriever's permission filter to
        return Miro chunks in search results.

        Without this, the syncer wrote zero relationships, the retriever's
        ``lookup_resources`` query (retriever.py:1281) returned no doc_ids
        for the user, and every Miro chunk was silently filtered out — even
        though the chunks were correctly stored in Milvus. Symptom was
        Postgres ``total_chunks > 0`` with zero hits from /search.
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
        if not self._client:
            return False
        try:
            await self._client.get("/boards", params={"limit": 1})
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


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*\n[ \t]*(\n)+")


def _strip_html(s: str) -> str:
    """Convert Miro's HTML item content to clean plain text.

    Miro returns rich-text item content as HTML (``<h1>…</h1><p>…</p>``).
    Indexing that raw bloats chunks with markup and hurts retrieval, so
    we turn block-level tags into newlines, drop remaining tags, and
    unescape the common entities. Returns ``s`` unchanged when there's
    no markup.
    """
    if not s or "<" not in s:
        return s or ""
    # Block-level tags → newline so paragraphs/headings stay separated.
    s = re.sub(r"(?i)<\s*br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</\s*(p|div|h[1-6]|li|tr|ul|ol|table|section)\s*>", "\n", s)
    text = _TAG_RE.sub("", s)
    for ent, ch in (
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"'),
    ):
        text = text.replace(ent, ch)
    # Collapse runs of blank lines left behind by stripped tags.
    text = _WS_RE.sub("\n\n", text)
    return text.strip()


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
