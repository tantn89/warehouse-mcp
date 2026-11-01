"""Load the bundled Wikipedia Parquet snapshot into Postgres or BigQuery.

Postgres (default): connects as the schema owner, then for each table drops it, recreates it
with mapped types and fills it with COPY, all in one transaction, so a rerun is idempotent and
a failure leaves the previous load in place.

    docker compose up -d --wait
    python scripts/load_snapshot.py                       # local container defaults
    python scripts/load_snapshot.py --dsn postgresql://...  # or WAREHOUSE_MCP_LOADER_DSN

BigQuery: one load job per table straight from the Parquet files, replacing the table.
Credentials come from Application Default Credentials.

    python scripts/load_snapshot.py --target bigquery --project P --dataset D --location US

Timestamps are naive UTC in the snapshot and stay naive in Postgres (timestamp without time
zone), so results match DuckDB value for value.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

import duckdb

from warehouse_mcp.demo import TABLES

# The loader role from docker/postgres-init.sql; the password is a local-only placeholder.
DEFAULT_DSN = ("postgresql://warehouse_owner:local-only-owner-password"
               "@127.0.0.1:55432/warehouse")
DEFAULT_SCHEMA = "wikipedia"

# DuckDB column type (as read from the Parquet files) -> Postgres column type.
POSTGRES_TYPES = {
    "VARCHAR": "text",
    "BIGINT": "bigint",
    "INTEGER": "integer",
    "DOUBLE": "double precision",
    "BOOLEAN": "boolean",
    "DATE": "date",
    "TIMESTAMP": "timestamp without time zone",
}


@contextmanager
def parquet_files() -> Iterator[dict[str, Path]]:
    """Table name -> Parquet path, valid while the context is open (works from a wheel too)."""
    data = resources.files("warehouse_mcp.demo") / "data"
    with resources.as_file(data) as directory:
        paths = {table: Path(directory) / f"{table}.parquet" for table in TABLES}
        missing = [str(p) for p in paths.values() if not p.is_file()]
        if missing:
            raise SystemExit(f"snapshot files missing: {missing}")
        yield paths


def parquet_columns(con: duckdb.DuckDBPyConnection, path: Path) -> list[tuple[str, str]]:
    rows = con.execute("SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM "
                       "read_parquet(?))", [str(path)]).fetchall()
    return [(str(name), str(dtype)) for name, dtype in rows]


def postgres_type(table: str, column: str, duckdb_type: str) -> str:
    mapped = POSTGRES_TYPES.get(duckdb_type)
    if mapped is None:
        raise SystemExit(f"{table}.{column}: no Postgres mapping for DuckDB type {duckdb_type}")
    return mapped


def load_postgres(dsn: str, schema: str) -> None:
    # Imported here so the BigQuery path works with only the [bigquery] extra installed.
    import psycopg
    from psycopg import sql

    source = duckdb.connect()
    try:
        with parquet_files() as paths, psycopg.connect(dsn, autocommit=False) as conn:
            for table, path in paths.items():
                started = time.perf_counter()
                columns = parquet_columns(source, path)
                target = sql.Identifier(schema, table)
                column_defs = sql.SQL(", ").join(
                    sql.SQL("{} {}").format(sql.Identifier(name),
                                            sql.SQL(postgres_type(table, name, dtype)))
                    for name, dtype in columns)
                column_names = sql.SQL(", ").join(sql.Identifier(n) for n, _ in columns)
                conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(target))
                conn.execute(sql.SQL("CREATE TABLE {} ({})").format(target, column_defs))
                rows = source.execute("SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
                with conn.cursor() as cur, cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(
                        target, column_names)) as copy:
                    for row in rows:
                        copy.write_row(row)
                conn.execute(sql.SQL("ANALYZE {}").format(target))
                print(f"{schema}.{table}: {len(rows)} rows "
                      f"in {time.perf_counter() - started:.2f}s")
            conn.commit()
    finally:
        source.close()


def load_bigquery(project: str, dataset: str, location: str) -> None:
    """One WRITE_TRUNCATE Parquet load job per table (used for docs/bigquery-run.md)."""
    from google.cloud import bigquery

    client = bigquery.Client(project=project, location=location)
    try:
        target = bigquery.Dataset(f"{project}.{dataset}")
        target.location = location
        client.create_dataset(target, exists_ok=True)
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
        with parquet_files() as paths:
            for table, path in paths.items():
                with path.open("rb") as fh:
                    job = client.load_table_from_file(
                        fh, f"{project}.{dataset}.{table}", job_config=job_config)
                job.result()  # raises on a failed load
                print(f"{project}.{dataset}.{table}: {job.output_rows} rows")
    finally:
        client.close()  # type: ignore[no-untyped-call]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Load the bundled Wikipedia Parquet snapshot into Postgres or BigQuery.")
    parser.add_argument("--target", choices=["postgres", "bigquery"], default="postgres")
    parser.add_argument("--dsn", default=os.environ.get("WAREHOUSE_MCP_LOADER_DSN", DEFAULT_DSN),
                        help="Postgres DSN of the schema owner (env WAREHOUSE_MCP_LOADER_DSN).")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, help="Postgres schema to load.")
    parser.add_argument("--project", help="BigQuery project.")
    parser.add_argument("--dataset", help="BigQuery dataset, created if missing.")
    parser.add_argument("--location", default="US", help="BigQuery dataset location.")
    args = parser.parse_args(argv)

    if args.target == "bigquery":
        if not (args.project and args.dataset):
            parser.error("--target bigquery needs --project and --dataset")
        load_bigquery(args.project, args.dataset, args.location)
        return
    import psycopg

    try:
        load_postgres(args.dsn, args.schema)
    except psycopg.Error as exc:
        sys.exit(f"load_snapshot: Postgres error: {exc}")


if __name__ == "__main__":
    main()
