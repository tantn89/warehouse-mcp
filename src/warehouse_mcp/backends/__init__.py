"""Warehouse backends. Optional drivers are imported only when their connection is used."""

from __future__ import annotations

from warehouse_mcp.backends.base import Backend, BackendError, QueryResult
from warehouse_mcp.config import BigQueryConnection, Connection, DuckDBConnection

__all__ = ["Backend", "BackendError", "QueryResult", "open_backend"]


def open_backend(connection: Connection) -> Backend:
    if isinstance(connection, DuckDBConnection):
        from warehouse_mcp.backends.duckdb_backend import DuckDBBackend

        return DuckDBBackend(connection.path)
    if isinstance(connection, BigQueryConnection):
        from warehouse_mcp.backends.bigquery_backend import BigQueryBackend

        return BigQueryBackend(connection.project, connection.location,
                               connection.maximum_bytes_billed)
    from warehouse_mcp.backends.postgres_backend import PostgresBackend

    return PostgresBackend(connection.dsn_env)
