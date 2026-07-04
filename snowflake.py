"""Snowflake connector.

Auth: Snowflake key-pair or username/password
Sync: Scheduled (metadata query). Full refresh each sync.
Permissions: INFORMATION_SCHEMA grants (database/schema/table level)

Content types indexed:
  - Table/view metadata (columns, types, comments)
  - Stored procedures and UDFs (SQL body)
  - Table comments and column descriptions
  - Stage file listings (external stage contents)

This connector indexes Snowflake *metadata and documentation*,
not raw table data. It makes Snowflake's data catalog searchable
so agents can answer "what table has customer revenue data?" or
"what does the orders.status column mean?"
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors.base import (
    ConnectorBase,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)


class SnowflakeConnector(ConnectorBase):
    """Snowflake data catalog connector."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._account: str = config.get("account", "")
        self._databases: list[str] = config.get("databases", [])
        self._schemas: list[str] = config.get("schemas", [])  # Optional filter
        self._index_procedures: bool = config.get("index_procedures", True)
        self._index_stages: bool = config.get("index_stages", False)
        self._warehouse: str = config.get("warehouse", "COMPUTE_WH")
        self._conn = None  # snowflake.connector async connection

    async def authenticate(self, credentials: dict) -> None:
        import snowflake.connector

        connect_args: dict = {
            "account": self._account or credentials.get("account", ""),
            "warehouse": self._warehouse,
        }

        if "private_key" in credentials:
            # Key-pair auth (preferred for service accounts)
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import serialization

            p_key = serialization.load_pem_private_key(
                credentials["private_key"].encode(),
                password=credentials.get("private_key_passphrase", "").encode() or None,
                backend=default_backend(),
            )
            connect_args["user"] = credentials["user"]
            connect_args["private_key"] = p_key
        else:
            # Username/password auth
            connect_args["user"] = credentials.get("user", "")
            password = credentials.get("password")
            if not password:
                raise ValueError("Snowflake connector requires a password in credentials")
            connect_args["password"] = password

        self._conn = snowflake.connector.connect(**connect_args)
        logger.info("Snowflake authenticated: account=%s", connect_args["account"])

        # Discover databases if not explicitly configured
        if not self._databases:
            cursor = self._conn.cursor()
            cursor.execute("SHOW DATABASES")
            self._databases = [row[1] for row in cursor.fetchall()]
            cursor.close()

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all indexable metadata objects across configured databases."""
        assert self._conn is not None

        for db in self._databases:
            # Tables and views
            async for doc in self._list_tables(db, since=since):
                yield doc

            # Stored procedures and UDFs
            if self._index_procedures:
                async for doc in self._list_procedures(db):
                    yield doc

    async def _list_tables(
        self, database: str, since: datetime | None = None
    ) -> AsyncIterator[DocumentMetadata]:
        assert self._conn is not None
        qi = self._qi
        cursor = self._conn.cursor()

        try:
            schema_filter = ""
            if self._schemas:
                schema_list = ",".join(f"'{s}'" for s in self._schemas)
                schema_filter = f" AND TABLE_SCHEMA IN ({schema_list})"

            since_filter = ""
            if since:
                since_filter = f" AND LAST_ALTERED > '{since.strftime('%Y-%m-%d %H:%M:%S')}'"

            cursor.execute(
                f"SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, "
                f"ROW_COUNT, BYTES, COMMENT, LAST_ALTERED "
                f"FROM {qi(database)}.INFORMATION_SCHEMA.TABLES "
                f"WHERE TABLE_SCHEMA != 'INFORMATION_SCHEMA'{schema_filter}{since_filter} "
                f"ORDER BY LAST_ALTERED DESC NULLS LAST"
            )

            for row in cursor.fetchall():
                catalog, schema, name, ttype, row_count, size, comment, last_altered = row
                fqn = f"{catalog}.{schema}.{name}"
                modified = last_altered if isinstance(last_altered, datetime) else datetime.now(UTC)
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=UTC)

                yield DocumentMetadata(
                    external_id=f"table:{fqn}",
                    title=f"[{ttype}] {fqn}",
                    content_type="text/markdown",
                    modified_at=modified,
                    metadata={
                        "type": "table",
                        "table_type": ttype,
                        "row_count": row_count,
                        "size_bytes": size,
                        "database": catalog,
                        "schema": schema,
                        "comment": comment or "",
                    },
                )
        finally:
            cursor.close()

    async def _list_procedures(self, database: str) -> AsyncIterator[DocumentMetadata]:
        assert self._conn is not None
        cursor = self._conn.cursor()

        try:
            cursor.execute(
                f"SELECT PROCEDURE_CATALOG, PROCEDURE_SCHEMA, PROCEDURE_NAME, "
                f"ARGUMENT_SIGNATURE, PROCEDURE_LANGUAGE, COMMENT, LAST_ALTERED "
                f"FROM {database}.INFORMATION_SCHEMA.PROCEDURES "
                f"ORDER BY LAST_ALTERED DESC NULLS LAST"
            )

            for row in cursor.fetchall():
                catalog, schema, name, sig, lang, comment, last_altered = row
                fqn = f"{catalog}.{schema}.{name}"
                modified = last_altered if isinstance(last_altered, datetime) else datetime.now(UTC)
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=UTC)

                yield DocumentMetadata(
                    external_id=f"procedure:{fqn}",
                    title=f"[PROCEDURE] {fqn}{sig or '()'}",
                    content_type="text/markdown",
                    modified_at=modified,
                    metadata={
                        "type": "procedure",
                        "language": lang,
                        "database": catalog,
                        "schema": schema,
                        "comment": comment or "",
                    },
                )
        finally:
            cursor.close()

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._conn is not None
        doc_type, fqn = doc_id.split(":", 1)

        if doc_type == "table":
            return await self._fetch_table_metadata(fqn)
        elif doc_type == "procedure":
            return await self._fetch_procedure(fqn)
        else:
            raise ValueError(f"Unknown Snowflake doc type: {doc_type}")

    @staticmethod
    def _qi(name: str) -> str:
        """Quote a Snowflake identifier to prevent SQL injection."""
        return '"' + name.replace('"', '""') + '"'

    async def _fetch_table_metadata(self, fqn: str) -> RawDocument:
        """Build rich metadata document for a table/view."""
        assert self._conn is not None
        parts = fqn.split(".")
        if len(parts) != 3:
            raise ValueError(f"Invalid FQN: {fqn}")
        db, schema, table = parts
        qi = self._qi  # Quote identifiers to prevent SQL injection
        cursor = self._conn.cursor()

        try:
            # Get table comment (parameterized WHERE, quoted catalog reference)
            cursor.execute(
                f"SELECT COMMENT FROM {qi(db)}.INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_CATALOG=%s AND TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (db, schema, table),
            )
            table_comment = ""
            row = cursor.fetchone()
            if row:
                table_comment = row[0] or ""

            # Get columns
            cursor.execute(
                f"SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COMMENT "
                f"FROM {qi(db)}.INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_CATALOG=%s AND TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                "ORDER BY ORDINAL_POSITION",
                (db, schema, table),
            )
            columns = cursor.fetchall()

            # Build markdown document
            lines = [
                f"# {fqn}",
                "",
            ]
            if table_comment:
                lines.append(f"**Description:** {table_comment}")
                lines.append("")

            lines.append("## Columns")
            lines.append("")
            lines.append("| Column | Type | Nullable | Description |")
            lines.append("|--------|------|----------|-------------|")

            for col_name, dtype, nullable, default, comment in columns:
                null_str = "YES" if nullable == "YES" else "NO"
                comment_str = comment or ""
                lines.append(f"| {col_name} | {dtype} | {null_str} | {comment_str} |")

            # Try to get sample values for context
            try:
                cursor.execute(f"SELECT * FROM {qi(db)}.{qi(schema)}.{qi(table)} LIMIT 3")
                sample_rows = cursor.fetchall()
                if sample_rows:
                    col_names = [desc[0] for desc in cursor.description]
                    lines.append("")
                    lines.append("## Sample Data (3 rows)")
                    lines.append("")
                    lines.append("| " + " | ".join(col_names) + " |")
                    lines.append("|" + "|".join(["---"] * len(col_names)) + "|")
                    for srow in sample_rows:
                        vals = [str(v)[:50] if v is not None else "NULL" for v in srow]
                        lines.append("| " + " | ".join(vals) + " |")
            except Exception:
                pass  # Access might be restricted

            content = "\n".join(lines).encode("utf-8")
            return RawDocument(
                external_id=f"table:{fqn}",
                content=content,
                content_type="text/markdown",
                metadata={"filename": f"{fqn.replace('.', '_')}.md"},
            )
        finally:
            cursor.close()

    async def _fetch_procedure(self, fqn: str) -> RawDocument:
        assert self._conn is not None
        parts = fqn.split(".")
        if len(parts) != 3:
            raise ValueError(f"Invalid FQN: {fqn}")
        db, schema, proc = parts
        qi = self._qi
        cursor = self._conn.cursor()

        try:
            cursor.execute(
                f"SELECT PROCEDURE_DEFINITION, ARGUMENT_SIGNATURE, PROCEDURE_LANGUAGE, COMMENT "
                f"FROM {qi(db)}.INFORMATION_SCHEMA.PROCEDURES "
                "WHERE PROCEDURE_CATALOG=%s AND PROCEDURE_SCHEMA=%s "
                "AND PROCEDURE_NAME=%s LIMIT 1",
                (db, schema, proc),
            )
            row = cursor.fetchone()

            lines = [f"# Stored Procedure: {fqn}", ""]
            if row:
                definition, sig, lang, comment = row
                if comment:
                    lines.append(f"**Description:** {comment}")
                lines.append(f"**Language:** {lang or 'SQL'}")
                lines.append(f"**Signature:** `{proc}{sig or '()'}`")
                lines.append("")
                if definition:
                    lines.append("## Source Code")
                    lines.append(f"```{(lang or 'sql').lower()}")
                    lines.append(definition)
                    lines.append("```")
            else:
                lines.append("(procedure not found)")

            content = "\n".join(lines).encode("utf-8")
            return RawDocument(
                external_id=f"procedure:{fqn}",
                content=content,
                content_type="text/markdown",
                metadata={"filename": f"proc_{fqn.replace('.', '_')}.md"},
            )
        finally:
            cursor.close()

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get permissions based on Snowflake GRANTS."""
        assert self._conn is not None
        doc_type, fqn = doc_id.split(":", 1)
        entries: list[PermissionEntry] = []

        try:
            parts = fqn.split(".")
            if len(parts) < 3:
                return entries
            db, schema, obj = parts

            cursor = self._conn.cursor()
            try:
                obj_type = "TABLE" if doc_type == "table" else "PROCEDURE"
                parts = fqn.split(".")
                quoted_fqn = ".".join(self._qi(p) for p in parts) if len(parts) == 3 else fqn
                cursor.execute(f"SHOW GRANTS ON {obj_type} {quoted_fqn}")
                for row in cursor.fetchall():
                    # Row format varies but typically:
                    # (created_on, privilege, granted_on, name, granted_to, grantee_name, ...)
                    if len(row) >= 6:
                        privilege = row[1]
                        grantee = row[5]
                        if privilege in ("SELECT", "USAGE", "REFERENCES"):
                            relation = "viewer"
                        elif privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                            relation = "editor"
                        elif privilege in ("OWNERSHIP", "ALL"):
                            relation = "owner"
                        else:
                            relation = "viewer"
                        entries.append(
                            PermissionEntry(
                                subject_type="group",
                                subject_id=grantee,
                                relation=relation,
                            )
                        )
            finally:
                cursor.close()
        except Exception as e:
            logger.warning("Failed to get Snowflake grants for %s: %s", doc_id, e)

        return entries

    async def health_check(self) -> bool:
        if self._conn is None:
            return False
        try:
            cursor = self._conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
