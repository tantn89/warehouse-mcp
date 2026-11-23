"""Load the bundled Wikipedia Parquet snapshot into Snowflake, as the owner role.

Used by `scripts/load_snapshot.py --target snowflake`. Each table is recreated with explicit
types, its Parquet file is PUT into the table's own stage and loaded with COPY INTO, matching
columns by name. The table stage needs no grant beyond owning the table, so the reader role
never gets a stage, and reading is still only the SELECT that docker/snowflake-setup.sql grants
on future tables.

Snowflake commits each DDL statement on its own, so a load is idempotent per table rather than
one transaction: rerun it after a failure.

Timestamps are naive UTC in the snapshot and are loaded as TIMESTAMP_NTZ, so results match
DuckDB value for value; the backend pins the session's TIMEZONE and TIMESTAMP_TYPE_MAPPING.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import duckdb
import snowflake.connector
from load_snapshot import parquet_columns, parquet_files

OWNER_KEY_ENV = "WAREHOUSE_MCP_SNOWFLAKE_OWNER_KEY_FILE"
READER_ROLE = "MCP_READER"
READER_USER = "MCP_READER_SVC"

# DuckDB column type (as read from the Parquet files) -> Snowflake column type.
SNOWFLAKE_TYPES = {
    "VARCHAR": "VARCHAR",
    "BIGINT": "NUMBER(19, 0)",
    "INTEGER": "NUMBER(10, 0)",
    "DOUBLE": "FLOAT",
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP_NTZ",
}


def snowflake_type(table: str, column: str, duckdb_type: str) -> str:
    mapped = SNOWFLAKE_TYPES.get(duckdb_type)
    if mapped is None:
        raise SystemExit(f"{table}.{column}: no Snowflake mapping for DuckDB type {duckdb_type}")
    return mapped


def load_snowflake(account: str, user: str, role: str, warehouse: str, database: str,
                   schema: str) -> None:
    if role.upper() == READER_ROLE or user.upper() == READER_USER:
        raise SystemExit(f"load as the owner, never as {READER_USER} or {READER_ROLE}: the "
                         "reader must not be able to write")
    key_file = os.environ.get(OWNER_KEY_ENV)
    if not key_file or not os.path.isfile(key_file):
        raise SystemExit(f"set {OWNER_KEY_ENV} to the owner's private key file")
    try:
        conn = snowflake.connector.connect(
            account=account, user=user, role=role, warehouse=warehouse, database=database,
            schema=schema, authenticator="SNOWFLAKE_JWT", private_key_file=key_file,
            client_session_keep_alive=False, login_timeout=30,
            application="warehouse-mcp-loader",
            session_parameters={"QUERY_TAG": "warehouse-mcp-loader", "TIMEZONE": "UTC"})
    except snowflake.connector.Error as exc:
        raise SystemExit(f"load_snapshot: Snowflake connection failed: {exc.raw_msg}") from exc
    source = duckdb.connect()
    try:
        # By name the checks above can be dodged (a quoted or renamed role); the session says
        # which role is really in use. Loading under the wrong role would also grant it
        # ownership of the tables.
        check_role(conn, role)
        with parquet_files() as paths:
            for table, path in paths.items():
                started = time.perf_counter()
                loaded = load_table(conn, source, table, path)
                print(f"{database}.{schema}.{table}: {loaded} rows "
                      f"in {time.perf_counter() - started:.2f}s")
    except snowflake.connector.Error as exc:
        raise SystemExit(f"load_snapshot: Snowflake error: {exc.raw_msg}") from exc
    finally:
        source.close()
        conn.close()


def check_role(conn: snowflake.connector.Connection, role: str) -> None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT CURRENT_ROLE()")
        row = cur.fetchone()
    finally:
        cur.close()
    current = str(row[0]) if isinstance(row, tuple) else ""
    if current.upper() != role.upper() or current.upper() == READER_ROLE:
        raise SystemExit(f"the session runs as role {current or '?'}, not {role}; refusing to load")


def load_table(conn: snowflake.connector.Connection, source: duckdb.DuckDBPyConnection,
               table: str, path: Path) -> int:
    """Recreate one table, load its Parquet file and check the row count against the file."""
    definitions = ", ".join(f"{name} {snowflake_type(table, name, dtype)}"
                            for name, dtype in parquet_columns(source, path))
    cur = conn.cursor()
    try:
        cur.execute(f"CREATE OR REPLACE TABLE {table} ({definitions})")
        cur.execute(f"PUT 'file://{path.as_posix()}' @%{table} "
                    "OVERWRITE = TRUE AUTO_COMPRESS = FALSE")
        cur.execute(f"COPY INTO {table} FROM @%{table} "
                    "FILE_FORMAT = (TYPE = PARQUET USE_LOGICAL_TYPE = TRUE) "
                    "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE PURGE = TRUE "
                    "ON_ERROR = ABORT_STATEMENT")
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
    finally:
        cur.close()
    loaded = row[0] if isinstance(row, tuple) else None
    expected = source.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
    if not isinstance(loaded, int) or expected is None or loaded != expected[0]:
        raise SystemExit(f"{table}: loaded {loaded} rows, the snapshot has "
                         f"{expected[0] if expected else '?'}")
    return loaded
