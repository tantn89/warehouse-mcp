"""Snowflake: a SELECT-only service user, key-pair auth, and a server-side statement timeout.

Connect as a TYPE=SERVICE user whose role holds USAGE on one warehouse, database and schema
and SELECT on the exposed tables, nothing else (see docker/snowflake-setup.sql). Writes then
fail on grants, whatever the SQL says. Cost is bounded by the warehouse itself: an XS size,
AUTO_SUSPEND, a resource monitor and STATEMENT_TIMEOUT_IN_SECONDS, which this backend also
sets per session and per statement.

The session has no current database or schema. Dataset tables must be fully qualified in the
config (database.schema.table), so a reference the guard failed to rewrite cannot resolve to a
raw table: it fails for lack of a current schema.

The private key is read from a file whose path comes from an environment variable; the config
names the variable, never the key. Snowflake bills by warehouse time, not bytes scanned, so
`bytes_processed` stays None: the connector reports no scan statistics for a SELECT, and
fetching them from QUERY_HISTORY would cost a second query per call.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from warehouse_mcp.backends.base import BackendError, QueryResult
from warehouse_mcp.config import SnowflakeConnection

try:
    import snowflake.connector
    from snowflake.connector import Connection
    from snowflake.connector import SnowflakeCursor as Cursor
    from snowflake.connector import errors as snowflake_errors
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError("Snowflake support needs: pip install 'warehouse-mcp[snowflake]'") from exc

T = TypeVar("T")
QUERY_TAG = "warehouse-mcp"
# "Statement reached its statement or warehouse timeout of N second(s) and was canceled."
_TIMEOUT_ERRNO = 630
_TIMEOUT_SQLSTATE = "57014"
_OBJECT_MISSING_ERRNO = 2003  # "Object ... does not exist or not authorized."
# The session is gone, so the statement never ran: 390112 session expired, 390114
# authentication token expired, 250002 the connector's "Connection is closed".
_SESSION_GONE_ERRNOS = frozenset({390112, 390114, 250002})
# A physical table from the config: database.schema.table, unquoted identifiers only.
_TABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){2}")
# Snowflake messages can carry the account host and query or request ids. None of them help an
# agent fix its query, and the host and ids identify the deployment.
_HOST = re.compile(r"\b[\w.-]+\.snowflakecomputing\.(com|cn)\S*", re.IGNORECASE)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                   re.IGNORECASE)


class SnowflakeBackend:
    dialect = "snowflake"

    def __init__(self, connection: SnowflakeConnection) -> None:
        env = connection.private_key_file_env
        key_file = os.environ.get(env)
        if not key_file:
            raise BackendError(f"Set {env} to the path of the Snowflake private key file.")
        if not Path(key_file).is_file():
            raise BackendError(f"The private key file named by {env} does not exist.")
        self._settings = connection
        self._key_file = key_file
        self._max_timeout_s = connection.statement_timeout_s
        self._lock = threading.Lock()
        self._conn = self._connect()

    def _connect(self) -> Connection:
        settings = self._settings
        try:
            return snowflake.connector.connect(
                account=settings.account, user=settings.user, role=settings.role,
                warehouse=settings.warehouse, authenticator="SNOWFLAKE_JWT",
                private_key_file=self._key_file,
                # No heartbeat: it would hold a session open on the account for as long as the
                # server runs, idle or not. An expired session is replaced on the next call
                # instead (see _call), which costs one login after a long idle period.
                client_session_keep_alive=False,
                login_timeout=30,
                # Client-side backstop if the server-side cancel never arrives.
                query_timeout=settings.statement_timeout_s + 15,
                application=QUERY_TAG,
                session_parameters={
                    "QUERY_TAG": QUERY_TAG,
                    "STATEMENT_TIMEOUT_IN_SECONDS": settings.statement_timeout_s,
                    # Pinned so results and literals do not depend on account defaults: the
                    # snapshot's times are naive UTC, loaded as TIMESTAMP_NTZ.
                    "TIMEZONE": "UTC",
                    "TIMESTAMP_TYPE_MAPPING": "TIMESTAMP_NTZ",
                })
        except snowflake_errors.Error as exc:
            raise BackendError(f"Could not connect to Snowflake: {self._message(exc)}") from exc

    def _call(self, work: Callable[[Cursor], T]) -> T:
        """Run `work` on a fresh cursor; once, on a new session if the old one is gone.

        Only the errors that mean the session no longer exists trigger the retry, so the
        statement did not run; it is a read in any case. The lock makes concurrent calls that
        hit the same dead session reconnect once, not once each.
        """
        conn = self._conn
        try:
            return self._on_cursor(conn, work)
        except snowflake_errors.Error as exc:
            if exc.errno not in _SESSION_GONE_ERRNOS:
                raise
        with self._lock:
            if self._conn is conn:
                with contextlib.suppress(snowflake_errors.Error):  # already unusable
                    conn.close()
                self._conn = self._connect()
            conn = self._conn
        return self._on_cursor(conn, work)

    @staticmethod
    def _on_cursor(conn: Connection, work: Callable[[Cursor], T]) -> T:
        # One cursor per call: the connection is shared across worker threads, cursors are not.
        cursor = conn.cursor()
        try:
            return work(cursor)
        finally:
            cursor.close()

    def execute(self, sql: str, max_rows: int, timeout_s: float) -> QueryResult:
        # Snowflake counts whole seconds; never exceed the session cap set at connect time.
        seconds = max(1, min(math.ceil(timeout_s), self._max_timeout_s))

        def run(cursor: Cursor) -> QueryResult:
            # The connector accepts but ignores execute(timeout=...); a statement parameter is
            # enforced by the server, which cancels the query and frees the warehouse.
            cursor.set_statement_parameter("STATEMENT_TIMEOUT_IN_SECONDS", seconds)
            cursor.execute(sql)
            rows = cursor.fetchmany(max_rows)
            # Snowflake upper-cases unquoted names (EDITOR, COUNT(*)). Every name is lower-cased,
            # quoted mixed-case aliases too: agents see the config's lower-case column names,
            # and results match DuckDB, Postgres and BigQuery column for column. Two aliases
            # that differ only by case come back with the same name; nothing keys on them.
            columns = [str(d[0]).lower() for d in cursor.description or []]
            return QueryResult(columns=columns, rows=[tuple(row) for row in rows])

        try:
            return self._call(run)
        except snowflake_errors.Error as exc:
            if exc.errno == _TIMEOUT_ERRNO or exc.sqlstate == _TIMEOUT_SQLSTATE:
                raise BackendError(f"Query exceeded the {seconds}s limit; narrow the time "
                                   "range or aggregate more.") from exc
            raise BackendError(self._message(exc)) from exc

    def table_columns(self, table: str) -> dict[str, str]:
        if not _TABLE.fullmatch(table):
            raise BackendError(f"Table {table!r} must be database.schema.table with unquoted "
                               "identifiers: the session has no current database or schema.")

        def describe(cursor: Cursor) -> dict[str, str]:
            cursor.execute(f"DESCRIBE TABLE {table}")
            # DESCRIBE TABLE rows start with (name, type, kind, ...).
            return {str(row[0]).lower(): str(row[1]) for row in cursor.fetchall()}

        try:
            return self._call(describe)
        except snowflake_errors.Error as exc:
            if exc.errno == _OBJECT_MISSING_ERRNO:
                return {}  # the service reports a missing table as a config mismatch
            raise BackendError(self._message(exc)) from exc

    def close(self) -> None:
        self._conn.close()

    def _message(self, exc: snowflake_errors.Error) -> str:
        """The engine's own words, minus the account host, ids, the account and the user.

        `raw_msg` already omits the error code, SQLSTATE and query id that `str(exc)` appends.
        The service then drops quoted values and redacts what is left.
        """
        text = " ".join(str(exc.raw_msg or "").split()) or "Query failed."
        text = _UUID.sub("<id>", _HOST.sub("<snowflake host>", text))
        for secret, label in ((self._settings.account, "<account>"),
                              (self._settings.user, "<user>")):
            text = re.sub(re.escape(secret), label, text, flags=re.IGNORECASE)
        return text
