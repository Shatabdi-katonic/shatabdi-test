"""Notion connector.

API: Notion API v2022-06-28
Auth: OAuth 2.0 (Notion integration) or Internal Integration Token
Sync: Incremental (last_edited_time filter) + full
Permissions: Notion page/database sharing (user, group, workspace)

Content types indexed:
  - Pages (with all nested blocks rendered as markdown)
  - Database entries (properties + page content)

Role mapping:
  full_access   -> editor
  can_edit      -> editor
  can_comment   -> viewer
  can_view      -> viewer
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

NOTION_API = "https://api.notion.com"
NOTION_VERSION = "2022-06-28"

ROLE_MAP = {
    "full_access": "editor",
    "can_edit": "editor",
    "can_comment": "viewer",
    "can_view": "viewer",
    "read_and_write": "editor",
    "read": "viewer",
}


class NotionConnector(ConnectorBase):
    """Notion connector for pages and database entries."""

    CONFIG_SCHEMA = [
        ConfigField(
            key="notionPageIds",
            label="Root page IDs",
            type="text",
            required=False,
            placeholder="e.g. 7f3c…, 9a2b…",
            help_text="Comma-separated page IDs to use as sync roots. Leave blank to discover pages via the integration's granted access.",
        ),
        ConfigField(
            key="index_databases",
            label="Index databases",
            type="boolean",
            required=False,
            default=True,
            help_text="When enabled, Notion databases are indexed alongside pages.",
        ),
        ConfigField(
            key="max_depth",
            label="Max traversal depth",
            type="number",
            required=False,
            default=10,
            help_text="How many levels deep to recurse under each root page.",
        ),
    ]

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Frontend field: notionPageIds (comma-separated) as fallback for root_page_ids
        root_ids = config.get("root_page_ids", [])
        if not root_ids:
            raw_ids = config.get("notionPageIds", "")
            if raw_ids:
                root_ids = [pid.strip() for pid in raw_ids.split(",") if pid.strip()]
        self._root_page_ids: list[str] = root_ids
        self._database_ids: list[str] = config.get("database_ids", [])
        self._index_databases: bool = config.get("index_databases", True)
        self._max_depth: int = config.get("max_depth", 10)
        # Frontend field: notionToken as fallback
        self._notion_token: str = config.get("notionToken", "")
        self._client: RetryClient | None = None
        self._workspace_name: str = ""

    async def authenticate(self, credentials: dict) -> None:
        token = (
            credentials.get("access_token", "")
            or credentials.get("integration_token", "")
            or credentials.get("notionToken", "")
            or self._notion_token
        )
        if not token:
            raise ConnectorAuthError(
                "Notion connector requires access_token, integration_token, or notionToken"
            )

        self._client = RetryClient(
            base_url=NOTION_API,
            headers={
                **bearer_headers(token),
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity
        try:
            me = await self._client.get_json("/v1/users/me")
            bot = me.get("bot", {})
            self._workspace_name = bot.get("workspace_name", "unknown")
            logger.info("Notion authenticated for workspace: %s", self._workspace_name)
        except Exception as e:
            raise ConnectorAuthError(f"Notion authentication failed: {e}") from e

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all accessible pages and database entries."""
        assert self._client is not None

        # Use Notion search API to find all pages and databases
        # the integration has access to
        has_more = True
        start_cursor: str | None = None

        filter_body: dict = {"page_size": 100}
        if since:
            filter_body["filter"] = {"property": "object", "value": "page"}
            filter_body["sort"] = {
                "direction": "descending",
                "timestamp": "last_edited_time",
            }

        while has_more:
            if start_cursor:
                filter_body["start_cursor"] = start_cursor

            try:
                resp = await self._client.post("/v1/search", json=filter_body)
                data = resp.json()
            except Exception as e:
                logger.error("Notion search API failed: %s", e)
                raise

            for result in data.get("results", []):
                obj_type = result.get("object")
                obj_id = result.get("id", "")
                last_edited = _parse_notion_dt(result.get("last_edited_time", ""))

                if since and last_edited < since:
                    # Results sorted by last_edited desc; can stop early
                    has_more = False
                    break

                if obj_type == "page":
                    title = _extract_page_title(result)
                    yield DocumentMetadata(
                        external_id=f"page:{obj_id}",
                        title=title,
                        url=result.get("url"),
                        content_type="text/markdown",
                        author=_extract_author(result),
                        modified_at=last_edited,
                        metadata={
                            "type": "page",
                            "parent_type": result.get("parent", {}).get("type", ""),
                        },
                    )

                elif obj_type == "database" and self._index_databases:
                    title = _extract_db_title(result)
                    yield DocumentMetadata(
                        external_id=f"database:{obj_id}",
                        title=f"[Database] {title}",
                        url=result.get("url"),
                        content_type="text/markdown",
                        author=_extract_author(result),
                        modified_at=last_edited,
                        metadata={"type": "database"},
                    )

            has_more = data.get("has_more", False) and has_more
            start_cursor = data.get("next_cursor")

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch page or database content as markdown."""
        assert self._client is not None

        obj_type, obj_id = doc_id.split(":", 1)

        if obj_type == "page":
            return await self._fetch_page(obj_id)
        elif obj_type == "database":
            return await self._fetch_database(obj_id)
        else:
            raise ValueError(f"Unknown Notion object type: {obj_type}")

    async def _fetch_page(self, page_id: str) -> RawDocument:
        """Fetch a page's metadata and all blocks as markdown."""
        assert self._client is not None

        # Get page metadata
        page = await self._client.get_json(f"/v1/pages/{page_id}")
        title = _extract_page_title(page)

        # Get all blocks (recursive)
        blocks = await self._get_all_blocks(page_id)
        md = _blocks_to_markdown(blocks)

        # Build full document
        lines = [f"# {title}", ""]
        if md.strip():
            lines.append(md)
        else:
            lines.append("(empty page)")

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=f"page:{page_id}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"{_slugify(title)}.md"},
        )

    async def _fetch_database(self, db_id: str) -> RawDocument:
        """Fetch database schema and all entries as markdown."""
        assert self._client is not None

        # Get database metadata
        db = await self._client.get_json(f"/v1/databases/{db_id}")
        title = _extract_db_title(db)

        # Query all entries
        lines = [f"# Database: {title}", ""]

        # Extract property schema for table header
        props = db.get("properties", {})
        prop_names = list(props.keys())[:20]  # Cap columns

        has_more = True
        start_cursor: str | None = None
        entry_count = 0

        while has_more and entry_count < 500:  # Cap entries
            body: dict = {"page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor

            resp = await self._client.post(f"/v1/databases/{db_id}/query", json=body)
            data = resp.json()

            for entry in data.get("results", []):
                entry_count += 1
                entry_title = _extract_page_title(entry)
                lines.append(f"## {entry_title}")

                # Extract property values
                for pname in prop_names:
                    pval = entry.get("properties", {}).get(pname, {})
                    rendered = _render_property(pval)
                    if rendered:
                        lines.append(f"- **{pname}:** {rendered}")

                # Fetch page content if it has blocks
                try:
                    blocks = await self._get_all_blocks(entry["id"])
                    md = _blocks_to_markdown(blocks)
                    if md.strip():
                        lines.append("")
                        lines.append(md)
                except Exception:
                    pass

                lines.append("")

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=f"database:{db_id}",
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"db-{_slugify(title)}.md", "entry_count": entry_count},
        )

    async def _get_all_blocks(self, block_id: str, depth: int = 0) -> list[dict]:
        """Recursively fetch all child blocks."""
        assert self._client is not None
        if depth > self._max_depth:
            return []

        blocks: list[dict] = []
        has_more = True
        start_cursor: str | None = None

        while has_more:
            params: dict = {"page_size": "100"}
            if start_cursor:
                params["start_cursor"] = start_cursor

            data = await self._client.get_json(
                f"/v1/blocks/{block_id}/children",
                params=params,
            )

            for block in data.get("results", []):
                blocks.append(block)
                # Recursively fetch children if block has_children
                if block.get("has_children", False):
                    children = await self._get_all_blocks(block["id"], depth + 1)
                    block["_children"] = children

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        return blocks

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get sharing permissions for a Notion page or database.

        Note: Notion API has limited permission visibility. We can see
        who the page is shared with but not all inherited permissions.
        """
        assert self._client is not None
        obj_type, obj_id = doc_id.split(":", 1)

        entries: list[PermissionEntry] = []

        try:
            if obj_type == "page":
                page = await self._client.get_json(f"/v1/pages/{obj_id}")
            else:
                page = await self._client.get_json(f"/v1/databases/{obj_id}")

            # Notion doesn't expose a direct permissions API,
            # but we can infer from the created_by and last_edited_by
            created_by = page.get("created_by", {})
            if created_by.get("type") == "person":
                person = created_by.get("person", {})
                email = person.get("email", "")
                if email:
                    entries.append(
                        PermissionEntry(
                            subject_type="user",
                            subject_id=email,
                            relation="owner",
                        )
                    )

            # For workspace-level pages, all workspace members have access
            parent = page.get("parent", {})
            if parent.get("type") == "workspace":
                entries.append(
                    PermissionEntry(
                        subject_type="group",
                        subject_id=f"workspace:{self._workspace_name}",
                        relation="viewer",
                    )
                )

        except Exception as e:
            logger.warning("Failed to get permissions for %s: %s", doc_id, e)

        return entries

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get_json("/v1/users/me")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


# ---------------------------------------------------------------------------
# Block-to-markdown rendering
# ---------------------------------------------------------------------------


def _blocks_to_markdown(blocks: list[dict], indent: int = 0) -> str:
    """Convert Notion blocks to markdown."""
    lines: list[str] = []
    prefix = "  " * indent

    for block in blocks:
        btype = block.get("type", "")
        bdata = block.get(btype, {})

        if btype == "paragraph":
            text = _rich_text_to_str(bdata.get("rich_text", []))
            lines.append(f"{prefix}{text}")

        elif btype.startswith("heading_"):
            level = int(btype[-1]) if btype[-1].isdigit() else 1
            text = _rich_text_to_str(bdata.get("rich_text", []))
            lines.append(f"{prefix}{'#' * (level + 1)} {text}")

        elif btype == "bulleted_list_item":
            text = _rich_text_to_str(bdata.get("rich_text", []))
            lines.append(f"{prefix}- {text}")

        elif btype == "numbered_list_item":
            text = _rich_text_to_str(bdata.get("rich_text", []))
            lines.append(f"{prefix}1. {text}")

        elif btype == "to_do":
            text = _rich_text_to_str(bdata.get("rich_text", []))
            checked = "[x]" if bdata.get("checked") else "[ ]"
            lines.append(f"{prefix}- {checked} {text}")

        elif btype == "toggle":
            text = _rich_text_to_str(bdata.get("rich_text", []))
            lines.append(f"{prefix}**{text}**")

        elif btype == "code":
            text = _rich_text_to_str(bdata.get("rich_text", []))
            lang = bdata.get("language", "")
            lines.append(f"{prefix}```{lang}")
            lines.append(text)
            lines.append(f"{prefix}```")

        elif btype == "quote":
            text = _rich_text_to_str(bdata.get("rich_text", []))
            lines.append(f"{prefix}> {text}")

        elif btype == "callout":
            icon = bdata.get("icon", {}).get("emoji", "")
            text = _rich_text_to_str(bdata.get("rich_text", []))
            lines.append(f"{prefix}> {icon} {text}")

        elif btype == "divider":
            lines.append(f"{prefix}---")

        elif btype == "table":
            # Table rendering handled separately
            children = block.get("_children", [])
            for row in children:
                cells = row.get("table_row", {}).get("cells", [])
                cell_texts = [_rich_text_to_str(c) for c in cells]
                lines.append(f"{prefix}| {' | '.join(cell_texts)} |")

        elif btype == "image":
            url = ""
            if bdata.get("type") == "external":
                url = bdata.get("external", {}).get("url", "")
            elif bdata.get("type") == "file":
                url = bdata.get("file", {}).get("url", "")
            caption = _rich_text_to_str(bdata.get("caption", []))
            lines.append(f"{prefix}![{caption}]({url})")

        elif btype == "bookmark":
            url = bdata.get("url", "")
            caption = _rich_text_to_str(bdata.get("caption", []))
            lines.append(f"{prefix}[{caption or url}]({url})")

        elif btype == "child_page":
            title = bdata.get("title", "Untitled")
            lines.append(f"{prefix}**[Sub-page: {title}]**")

        elif btype == "child_database":
            title = bdata.get("title", "Untitled")
            lines.append(f"{prefix}**[Sub-database: {title}]**")

        # Render children (for toggles, lists, etc.)
        children = block.get("_children", [])
        if children and btype != "table":
            child_md = _blocks_to_markdown(children, indent + 1)
            if child_md.strip():
                lines.append(child_md)

        lines.append("")

    return "\n".join(lines)


def _rich_text_to_str(rich_text: list[dict]) -> str:
    """Convert Notion rich_text array to plain text with basic formatting."""
    parts: list[str] = []
    for rt in rich_text:
        text = rt.get("plain_text", "")
        annotations = rt.get("annotations", {})

        if annotations.get("bold"):
            text = f"**{text}**"
        if annotations.get("italic"):
            text = f"*{text}*"
        if annotations.get("code"):
            text = f"`{text}`"
        if annotations.get("strikethrough"):
            text = f"~~{text}~~"

        href = rt.get("href")
        if href:
            text = f"[{text}]({href})"

        parts.append(text)

    return "".join(parts)


def _extract_page_title(page: dict) -> str:
    """Extract title from a Notion page object."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            title_parts = prop.get("title", [])
            return _rich_text_to_str(title_parts) or "Untitled"
    return "Untitled"


def _extract_db_title(db: dict) -> str:
    """Extract title from a Notion database object."""
    title_parts = db.get("title", [])
    return _rich_text_to_str(title_parts) or "Untitled Database"


def _extract_author(obj: dict) -> str | None:
    """Extract created_by email if available."""
    created_by = obj.get("created_by", {})
    if created_by.get("type") == "person":
        return created_by.get("person", {}).get("email")
    return None


def _render_property(prop: dict) -> str:
    """Render a Notion property value as a string."""
    ptype = prop.get("type", "")

    if ptype == "title":
        return _rich_text_to_str(prop.get("title", []))
    elif ptype == "rich_text":
        return _rich_text_to_str(prop.get("rich_text", []))
    elif ptype == "number":
        val = prop.get("number")
        return str(val) if val is not None else ""
    elif ptype == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    elif ptype == "multi_select":
        return ", ".join(s.get("name", "") for s in prop.get("multi_select", []))
    elif ptype == "date":
        date = prop.get("date")
        if date:
            return date.get("start", "")
        return ""
    elif ptype == "checkbox":
        return "Yes" if prop.get("checkbox") else "No"
    elif ptype == "url":
        return prop.get("url", "") or ""
    elif ptype == "email":
        return prop.get("email", "") or ""
    elif ptype == "phone_number":
        return prop.get("phone_number", "") or ""
    elif ptype == "status":
        status = prop.get("status")
        return status.get("name", "") if status else ""
    elif ptype == "people":
        return ", ".join(
            p.get("name", p.get("person", {}).get("email", "")) for p in prop.get("people", [])
        )
    elif ptype == "relation":
        return f"({len(prop.get('relation', []))} linked)"
    elif ptype == "formula":
        formula = prop.get("formula", {})
        ftype = formula.get("type", "")
        return str(formula.get(ftype, ""))
    else:
        return ""


def _parse_notion_dt(s: str) -> datetime:
    """Parse Notion datetime string."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _slugify(text: str) -> str:
    """Simple slug for filenames."""
    import re

    slug = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_]+", "-", slug).strip("-")[:60]
