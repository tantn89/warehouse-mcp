"""Postgres: every query in a read-only transaction with a server-side statement timeout.

Connect as a role that only holds SELECT on the exposed tables (see docker/postgres-init.sql).
The transaction mode and the grants then both refuse writes, whatever the SQL says.
"""

from __future__ import annotations

import os
from typing import Any

from warehouse_mcp.backends.base import BackendError, QueryResult

try:
    import psycopg
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError("Postgres support needs: pip install 'warehouse-mcp[postgres]'") from exc


class PostgresBackend:
    dialect = "postgres"

    def __init__(self, dsn_env: str) -> None:
        dsn = os.environ.get(dsn_env)
        if not dsn:
            raise BackendError(f"Set {dsn_env} to the Postgres connection string.")
        self._dsn = dsn

    def _connect(self, timeout_s: float) -> psycopg.Connection[Any]:
        ms = int(timeout_s * 1000)
        conn = psycopg.connect(self._dsn, connect_timeout=10, autocommit=False,
                               options=f"-c statement_timeout={ms} "
                                       "-c default_transaction_read_only=on")
        conn.read_only = True
        return conn

    def execute(self, sql: str, max_rows: int, timeout_s: float) -> QueryResult:
        try:
            with self._connect(timeout_s) as conn, conn.cursor() as cur:
                cur.execute(sql.encode())  # bytes: psycopg must not treat % as a placeholder
                rows = cur.fetchmany(max_rows)
                columns = [d.name for d in cur.description or []]
                conn.rollback()
        except psycopg.errors.QueryCanceled as exc:
            raise BackendError(f"Query exceeded the {timeout_s:.0f}s limit; narrow the time "
                               "range or aggregate more.") from exc
        except psycopg.Error as exc:
            message = exc.diag.message_primary if exc.diag else None
            raise BackendError(message or "Query failed.") from exc
        return QueryResult(columns=columns, rows=rows)

    def table_columns(self, table: str) -> dict[str, str]:
        schema, _, name = table.rpartition(".")
        with self._connect(10) as conn, conn.cursor() as cur:
            cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_name = %s AND table_schema = %s ORDER BY ordinal_position",
                        (name, schema or "public"))
            return {str(column): str(dtype) for column, dtype in cur.fetchall()}

    def close(self) -> None:
        return None
