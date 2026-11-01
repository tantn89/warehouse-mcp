"""DuckDB: a database file opened read-only, with every path to files and the network closed.

The configuration is locked after start, so a query cannot `SET` its way back to file access.
That makes the engine itself the second layer behind the SQL guard. DuckDB has no statement
timeout, so a timer interrupts queries that run too long.
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

from warehouse_mcp.backends.base import BackendError, QueryResult


class DuckDBBackend:
    dialect = "duckdb"

    def __init__(self, path: str) -> None:
        if not Path(path).is_file():
            raise BackendError(f"DuckDB database not found: {path}")
        self._con = duckdb.connect(path, read_only=True, config={
            "enable_external_access": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "lock_configuration": True,
        })

    def execute(self, sql: str, max_rows: int, timeout_s: float) -> QueryResult:
        cursor = self._con.cursor()  # one cursor per call: tool calls run on worker threads
        timer = threading.Timer(timeout_s, cursor.interrupt)
        timer.start()
        try:
            cursor.execute(sql)
            rows = cursor.fetchmany(max_rows)
            columns = [d[0] for d in cursor.description or []]
        except duckdb.InterruptException as exc:
            raise BackendError(f"Query exceeded the {timeout_s:.0f}s limit; narrow the time "
                               "range or aggregate more.") from exc
        except duckdb.Error as exc:
            raise BackendError(_first_line(str(exc))) from exc
        finally:
            timer.cancel()
            cursor.close()
        return QueryResult(columns=columns, rows=rows)

    def table_columns(self, table: str) -> dict[str, str]:
        schema, _, name = table.rpartition(".")
        rows = self._con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema = ? ORDER BY ordinal_position",
            [name, schema or "main"]).fetchall()
        return {str(column): str(dtype) for column, dtype in rows}

    def close(self) -> None:
        self._con.close()


def _first_line(message: str) -> str:
    # DuckDB errors carry a caret diagram of the (physical) query; the first line is enough.
    return message.strip().splitlines()[0] if message.strip() else "Query failed."
