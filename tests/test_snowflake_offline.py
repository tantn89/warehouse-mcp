"""Snowflake without an account: SQL rendering, pseudonym semantics, guard refusals.

The guard and the metrics compiler write Snowflake SQL. Two backends stand in for the
warehouse here, both test-only:
- `RefusingBackend` fails the test if a query reaches it, so a refusal is proven to happen in
  the guard;
- `SnowflakeSqlOnDuckDB` takes the Snowflake SQL, has sqlglot read it back as Snowflake and
  runs it on the DuckDB demo. Every parity case then has to give DuckDB's own answer, which
  checks that the rendering keeps the meaning, not only that it parses.
The live run (tests/test_snowflake_parity.py) checks the same cases on Snowflake itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest
import sqlglot
from sqlglot import exp

from test_postgres_parity import CASES, DEMO_CONFIG, _environment, normalized
from test_red_team import SNOWFLAKE_ATTACKS
from warehouse_mcp.audit import AuditLog
from warehouse_mcp.backends import Backend
from warehouse_mcp.backends.base import QueryResult
from warehouse_mcp.backends.duckdb_backend import DuckDBBackend
from warehouse_mcp.config import Settings, load_settings
from warehouse_mcp.demo import build_demo_database
from warehouse_mcp.semantic import compile_metrics_query
from warehouse_mcp.service import WarehouseService
from warehouse_mcp.sql_guard import QueryRejected, pseudonym

SALT = "offline0123456789salt"


class RefusingBackend:
    dialect = "snowflake"

    def execute(self, sql: str, max_rows: int, timeout_s: float) -> QueryResult:
        raise AssertionError(f"the guard let this reach the warehouse: {sql}")

    def table_columns(self, table: str) -> dict[str, str]:
        return {}

    def close(self) -> None:
        return None


class SnowflakeSqlOnDuckDB:
    dialect = "snowflake"

    def __init__(self, duck: DuckDBBackend) -> None:
        self._duck = duck
        self.statements: list[str] = []

    def execute(self, sql: str, max_rows: int, timeout_s: float) -> QueryResult:
        self.statements.append(sql)
        return self._duck.execute(sqlglot.transpile(sql, read="snowflake", write="duckdb")[0],
                                  max_rows, timeout_s)

    def table_columns(self, table: str) -> dict[str, str]:
        return self._duck.table_columns(table)

    def close(self) -> None:
        self._duck.close()


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Settings, Path]]:
    directory = tmp_path_factory.mktemp("snowflake-offline")
    database = build_demo_database(directory / "wikipedia.duckdb")
    with _environment(WAREHOUSE_MCP_DEMO_DB=str(database), WAREHOUSE_MCP_SCHEMA="main"):
        yield load_settings(DEMO_CONFIG), database


def _service(settings: Settings, backend: Backend, directory: Path) -> WarehouseService:
    return WarehouseService(settings, backend, AuditLog(str(directory / "audit.jsonl")),
                            pii_salt=SALT)


# --- Rendering: every parity case, through the guard, in the snowflake dialect ----------

@pytest.fixture(scope="module")
def engines(demo: tuple[Settings, Path], tmp_path_factory: pytest.TempPathFactory,
            ) -> Iterator[tuple[WarehouseService, WarehouseService, SnowflakeSqlOnDuckDB]]:
    settings, database = demo
    directory = tmp_path_factory.mktemp("snowflake-render")
    rendered = SnowflakeSqlOnDuckDB(DuckDBBackend(str(database)))
    duck = _service(settings, DuckDBBackend(str(database)), directory)
    yield duck, _service(settings, rendered, directory), rendered
    duck.backend.close()
    rendered.close()


@pytest.mark.parametrize("case", list(CASES))
def test_parity_cases_render_to_snowflake_and_keep_their_meaning(
        engines: tuple[WarehouseService, WarehouseService, SnowflakeSqlOnDuckDB],
        case: str) -> None:
    duck, snowflake_sql, rendered = engines
    expected, actual = CASES[case](duck), CASES[case](snowflake_sql)
    sql = rendered.statements[-1]
    sqlglot.parse_one(sql, read="snowflake")  # valid Snowflake, as far as sqlglot knows
    assert expected.row_count > 0
    assert normalized(expected) == normalized(actual)


# --- Pseudonyms: same values as DuckDB --------------------------------------------------

SAMPLES = ["FenaBot", "Alice", "Ünïcödé 名前", "12345", "", "with 'quote'"]


def test_pseudonym_renders_with_snowflake_semantics() -> None:
    sql = pseudonym("editor", SALT).sql(dialect="snowflake")
    # MD5 (hex, lower case), never MD5_BINARY; SUBSTRING is 1-based in both engines; the
    # cast is VARCHAR; CONCAT's NULL handling is made explicit, and the CASE keeps NULL as NULL.
    assert f"MD5(CONCAT('{SALT}', COALESCE(CAST(editor AS VARCHAR), '')))" in sql
    assert "SUBSTRING(" in sql and ", 1, 12)" in sql and "MD5_BINARY" not in sql
    assert sql.startswith("CASE WHEN editor IS NULL THEN NULL ELSE CONCAT('pii_', ")


@pytest.mark.parametrize("value", [*SAMPLES, None])
def test_pseudonym_values_match_duckdb_and_the_md5_definition(value: str | None) -> None:
    duckdb_sql = pseudonym("v", SALT).sql(dialect="duckdb")
    snowflake_sql = sqlglot.transpile(pseudonym("v", SALT).sql(dialect="snowflake"),
                                      read="snowflake", write="duckdb")[0]
    con = duckdb.connect()
    try:
        native, rendered = (con.execute(f"SELECT {sql} FROM (SELECT ?::VARCHAR AS v)",
                                        [value]).fetchone()[0]  # type: ignore[index]
                            for sql in (duckdb_sql, snowflake_sql))
    finally:
        con.close()
    # Snowflake's MD5 is the lower-case hex digest of the UTF-8 bytes, as hashlib computes it.
    reference = (None if value is None else
                 "pii_" + hashlib.md5((SALT + value).encode()).hexdigest()[:12])
    assert native == rendered == reference


# --- Time literals ----------------------------------------------------------------------

def test_time_range_literals_are_timestamp_ntz_without_time_zone_conversion(
        demo: tuple[Settings, Path]) -> None:
    edits = demo[0].dataset("edits")
    assert edits is not None
    bangkok = timezone(timedelta(hours=7))
    sql = compile_metrics_query(edits, ["edits"], [], [], "minute",
                                datetime(2026, 11, 1, 11, 30, tzinfo=bangkok),
                                datetime(2026, 11, 1, 4, 50), None, True, 10, "snowflake")
    assert "event_time >= CAST('2026-11-01 04:30:00' AS TIMESTAMPNTZ)" in sql
    assert "event_time < CAST('2026-11-01 04:50:00' AS TIMESTAMPNTZ)" in sql
    casts = list(sqlglot.parse_one(sql, read="snowflake").find_all(exp.Cast))
    assert casts and all(c.to.this == exp.DataType.Type.TIMESTAMPNTZ for c in casts)
    assert not any(word in sql.upper() for word in ("CONVERT_TIMEZONE", "TIMESTAMPTZ",
                                                    "TIMESTAMPLTZ", "AT TIME ZONE"))


# --- Guard refusals, in the snowflake dialect, before any backend -----------------------

@pytest.mark.parametrize("sql", SNOWFLAKE_ATTACKS)
def test_snowflake_attacks_are_refused_before_the_backend(demo: tuple[Settings, Path],
                                                          tmp_path: Path, sql: str) -> None:
    service = _service(demo[0], RefusingBackend(), tmp_path)
    with pytest.raises(QueryRejected):
        service.run_sql(service.settings.role("enwiki_partner"), sql)


def test_time_travel_refusal_names_the_reason(demo: tuple[Settings, Path],
                                              tmp_path: Path) -> None:
    service = _service(demo[0], RefusingBackend(), tmp_path)
    with pytest.raises(QueryRejected, match="Time travel"):
        service.run_sql(service.settings.role("analyst"), "SELECT * FROM edits AT(OFFSET => -1)")
