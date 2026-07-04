"""Database connector for PostgreSQL and MySQL.

Auth: Connection string (host, port, user, password, database)
Sync: Full (query execution) or incremental (WHERE updated_at > since)
Permissions: Source-level only (no per-row ACLs)

Each configured query produces one document per row. The connector
concatenates configured columns into text content for each row.

This is useful for knowledge bases stored in databases, CMS tables,
wiki tables, FAQ databases, etc.
"""

from __future__ import annotations

import json
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


class DatabaseConnector(ConnectorBase):
    """SQL database connector (PostgreSQL or MySQL).

    Config:
        db_type: "postgresql" or "mysql" (default: "postgresql")
        host: Database host
        port: Database port (default: 5432 for pg, 3306 for mysql)
        database: Database name
        queries: List of query configs, each with:
            - sql: SQL query (must return id, title, content columns at minimum)
            - name: Human-readable name for this query/table
            - id_column: Column to use as external_id (default: "id")
            - title_column: Column for document title (default: "title")
            - content_columns: List of columns to concatenate as content
            - modified_column: Column with last-modified timestamp (for incremental)
            - url_template: Optional URL template with {id} placeholder
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        # Accept BOTH the canonical keys (db_type/host/port/database/queries) and
        # the Add-Knowledge wizard's form keys (dbType/dbHost/dbPort/dbName/
        # dbQueries) — same field-mapping fallback the Linear/Outline/ClickUp
        # connectors use. Without this, a source created from the UI lands with
        # only the dbX keys, so the connector defaulted host→localhost,
        # database→"", queries→[] (0 docs) and had no password.
        self._db_type: str = config.get("db_type") or config.get("dbType") or "postgresql"
        self._host: str = config.get("host") or config.get("dbHost") or "localhost"
        _port = config.get("port") or config.get("dbPort")
        self._port: int = int(_port) if _port else (5432 if self._db_type == "postgresql" else 3306)
        self._database: str = config.get("database") or config.get("dbName") or ""
        self._queries: list[dict] = self._normalize_queries(
            config.get("queries") or config.get("dbQueries") or []
        )
        # Credentials may also arrive inline in config (the wizard sends
        # dbUser/dbPassword as config fields for api_key connectors). Hold them
        # as a fallback for authenticate()/_get_connection.
        self._config_user: str = config.get("dbUser") or config.get("user") or ""
        self._config_password: str = config.get("dbPassword") or config.get("password") or ""
        # CR-610: optional SSL mode (disable / require / prefer). The wizard has
        # no SSL field, so this is usually unset → we negotiate then fall back to
        # plaintext (see _get_connection). Non-SSL servers (local/dev Postgres,
        # bore.pub-style tunnels) otherwise raise "rejected SSL upgrade".
        self._ssl_mode: str = (
            config.get("db_sslmode")
            or config.get("dbSslMode")
            or config.get("sslmode")
            or ""
        ).strip().lower()
        self._conn = None
        self._credentials: dict = {}

    @staticmethod
    def _normalize_queries(raw) -> list[dict]:
        """Coerce the `queries` config into a list[dict].

        The wizard's "SQL Queries" textarea delivers a JSON **string**, not a
        parsed list. Parse it; if it isn't valid JSON, treat the text as a
        single bare SQL statement so a user who typed `SELECT * FROM t` still
        gets one query (id_column/title_column fall back to their defaults).
        """
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return []
            try:
                raw = json.loads(s)
            except (ValueError, TypeError):
                return [{"sql": s, "name": "query"}]
        if isinstance(raw, dict):
            raw = [raw]
        return [q for q in raw if isinstance(q, dict)] if isinstance(raw, list) else []

    async def authenticate(self, credentials: dict) -> None:
        self._credentials = credentials
        # Verify connectivity
        conn = await self._get_connection()
        if self._db_type == "postgresql":
            await conn.fetchval("SELECT 1")
            await conn.close()
        else:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
            conn.close()
        logger.info(
            "Database connected: %s@%s:%d/%s",
            credentials.get("user", ""),
            self._host,
            self._port,
            self._database,
        )

    async def _get_connection(self):
        """Create a database connection."""
        # Resolve creds from the credential store first, then fall back to the
        # config-level dbUser/dbPassword the wizard sends inline.
        user = (
            self._credentials.get("user")
            or self._credentials.get("dbUser")
            or self._config_user
            or ""
        )
        password = (
            self._credentials.get("password")
            or self._credentials.get("dbPassword")
            or self._config_password
            or None
        )
        if not password:
            raise ValueError("Database connector requires a password in credentials")

        if self._db_type == "postgresql":
            import asyncpg

            pg_args = dict(
                host=self._host,
                port=self._port,
                user=user,
                password=password,
                database=self._database,
            )
            # CR-610: honor an explicit sslmode; otherwise negotiate and fall
            # back to a non-SSL connection if the server refuses the SSLRequest
            # ("rejected SSL upgrade"). Without the fallback, any Postgres with
            # TLS disabled (dev DBs, bore.pub tunnels) is unreachable — which
            # made the Meridian source's re-embed error out and left its "stale"
            # badge stuck (the terminal flip to embedding_status="current" only
            # runs on a successful, idle-status sync).
            if self._ssl_mode in ("disable", "false", "off", "no"):
                return await asyncpg.connect(**pg_args, ssl=False)
            if self._ssl_mode in ("require", "verify-ca", "verify-full", "true", "on", "yes"):
                return await asyncpg.connect(**pg_args, ssl=True)
            try:
                return await asyncpg.connect(**pg_args)
            except Exception as exc:  # noqa: BLE001 - retry plaintext on SSL refusal
                # asyncpg surfaces a refused/garbled SSL upgrade in several ways
                # depending on the server/proxy: "rejected SSL upgrade", or a
                # torn-down transport during the handshake ("unexpected
                # connection_lost() call", "connection is closed"). The TCP
                # connection succeeded (a dead host gives "connection refused"),
                # so retry once WITHOUT SSL — this is the negotiation failing, not
                # an unreachable DB. Lets TLS-disabled Postgres (dev DBs,
                # bore.pub-style tunnels) connect.
                msg = str(exc).lower()
                if any(
                    s in msg
                    for s in ("ssl", "connection_lost", "connection is closed", "connection was closed")
                ):
                    # stdlib logging (not structlog) — use %-style args, not
                    # kwargs, or .warning() raises "unexpected keyword argument".
                    logger.warning(
                        "db_ssl_negotiation_failed_retrying_plaintext host=%s port=%s error=%s",
                        self._host,
                        self._port,
                        str(exc),
                    )
                    return await asyncpg.connect(**pg_args, ssl=False)
                raise
        else:
            import aiomysql

            return await aiomysql.connect(
                host=self._host,
                port=self._port,
                user=user,
                password=password,
                db=self._database,
            )

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        conn = await self._get_connection()

        try:
            for query_config in self._queries:
                sql = query_config["sql"]
                id_col = query_config.get("id_column", "id")
                title_col = query_config.get("title_column", "title")
                modified_col = query_config.get("modified_column")
                url_template = query_config.get("url_template")
                query_name = query_config.get("name", "query")

                # Add incremental filter if configured
                if since and modified_col:
                    # Validate modified_col is a simple identifier (prevent SQL injection)
                    import re
                    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_.]*$", modified_col):
                        raise ValueError(f"Invalid column name: {modified_col}")
                    # Wrap as subquery to safely add filter without modifying original SQL
                    sql = f"SELECT * FROM ({sql}) AS _incremental WHERE {modified_col} > $1"
                    params = [since]
                else:
                    params = []

                try:
                    if self._db_type == "postgresql":
                        rows = await conn.fetch(sql, *params)
                        rows = [dict(r) for r in rows]
                    else:
                        async with conn.cursor() as cur:
                            await cur.execute(sql, params or None)
                            columns = [d[0] for d in cur.description]
                            rows = [dict(zip(columns, row)) for row in await cur.fetchall()]

                    for row in rows:
                        row_id = str(row.get(id_col, ""))
                        if not row_id:
                            continue

                        ext_id = f"{query_name}:{row_id}"
                        title = str(row.get(title_col, ext_id))
                        url = url_template.format(id=row_id) if url_template else None

                        modified = datetime.now(UTC)
                        if modified_col and row.get(modified_col):
                            val = row[modified_col]
                            if isinstance(val, datetime):
                                modified = val
                            else:
                                try:
                                    modified = datetime.fromisoformat(str(val))
                                except ValueError:
                                    pass

                        yield DocumentMetadata(
                            external_id=ext_id,
                            title=title,
                            url=url,
                            content_type="text/plain",
                            modified_at=modified,
                            metadata={"query_name": query_name, "db_type": self._db_type},
                        )

                except Exception as e:
                    logger.error("Query '%s' failed: %s", query_name, e)
        finally:
            if self._db_type == "postgresql":
                await conn.close()
            else:
                conn.close()

    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Re-execute the query for a single row and build text content."""
        query_name, row_id = doc_id.split(":", 1)

        # Find the matching query config
        query_config = None
        for qc in self._queries:
            if qc.get("name", "query") == query_name:
                query_config = qc
                break

        if query_config is None:
            raise ValueError(f"No query config found for '{query_name}'")

        id_col = query_config.get("id_column", "id")
        content_cols = query_config.get("content_columns", [])
        sql = query_config["sql"]

        # Wrap in subquery to filter by ID
        wrapped = f"SELECT * FROM ({sql}) AS _sub WHERE {id_col} = $1"

        conn = await self._get_connection()
        try:
            if self._db_type == "postgresql":
                rows = await conn.fetch(wrapped, row_id)
                rows = [dict(r) for r in rows]
            else:
                async with conn.cursor() as cur:
                    await cur.execute(wrapped.replace("$1", "%s"), (row_id,))
                    columns = [d[0] for d in cur.description]
                    rows = [dict(zip(columns, row)) for row in await cur.fetchall()]
        finally:
            if self._db_type == "postgresql":
                await conn.close()
            else:
                conn.close()

        if not rows:
            raise ValueError(f"Row {doc_id} not found")

        row = rows[0]

        # Build text content from configured columns
        if content_cols:
            parts = [f"{col}: {row.get(col, '')}" for col in content_cols if row.get(col)]
        else:
            # Use all non-id columns
            parts = [f"{k}: {v}" for k, v in row.items() if k != id_col and v]

        content = "\n\n".join(parts)

        return RawDocument(
            external_id=doc_id,
            content=content.encode("utf-8"),
            content_type="text/plain",
            metadata={"query_name": query_name},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Database connector uses source-level permissions only."""
        return []

    async def health_check(self) -> bool:
        try:
            conn = await self._get_connection()
            if self._db_type == "postgresql":
                await conn.fetchval("SELECT 1")
                await conn.close()
            else:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                conn.close()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        pass
