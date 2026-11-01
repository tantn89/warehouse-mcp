from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from conftest import SALT, make_settings
from warehouse_mcp.config import Settings
from warehouse_mcp.sql_guard import QueryRejected, guard_query


def guard(sql: str, settings: Settings, role: str = "analyst", dialect: str = "duckdb"):
    return guard_query(sql, settings, settings.role(role), dialect, SALT)


def run(sql: str, db: Path, role: str = "analyst") -> list[tuple[object, ...]]:
    settings = make_settings(str(db))
    query = guard(sql, settings, role)
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(query.physical_sql).fetchall()
    finally:
        con.close()


# --- Allowed queries run and are rewritten -------------------------------------------------

def test_plain_select_runs_against_declared_columns(demo_db: Path) -> None:
    rows = run("SELECT wiki, count(*) FROM edits GROUP BY wiki ORDER BY wiki", demo_db)
    assert rows == [("dewiki", 1), ("enwiki", 3), ("frwiki", 1)]


def test_pii_is_pseudonymized_but_distinct_counts_stay_exact(demo_db: Path) -> None:
    names = run("SELECT DISTINCT editor FROM edits WHERE editor IS NOT NULL", demo_db)
    assert {n for (n,) in names}.isdisjoint({"Alice", "Bob", "CleanupBot"})
    assert all(str(n).startswith("pii_") and len(str(n)) == 16 for (n,) in names)
    assert run("SELECT count(DISTINCT editor) FROM edits", demo_db) == [(3,)]


def test_probing_a_name_matches_nothing(demo_db: Path) -> None:
    assert run("SELECT count(*) FROM edits WHERE editor = 'Alice'", demo_db) == [(0,)]


def test_cleared_role_sees_real_values(demo_db: Path) -> None:
    rows = run("SELECT DISTINCT editor FROM edits WHERE editor = 'Alice'", demo_db, role="admin")
    assert rows == [("Alice",)]


def test_row_filter_applies_inside_ctes_and_subqueries(demo_db: Path) -> None:
    sql = """WITH w AS (SELECT wiki FROM edits)
             SELECT (SELECT count(*) FROM edits) AS total, count(*) AS via_cte FROM w"""
    assert run(sql, demo_db, role="enwiki_only") == [(3, 3)]


def test_aliases_and_qualified_columns_survive_the_rewrite(demo_db: Path) -> None:
    rows = run("SELECT e.wiki FROM edits AS e JOIN edits f ON e.event_id = f.event_id "
               "WHERE e.is_bot ORDER BY 1", demo_db)
    assert rows == [("enwiki",)]


def test_limit_is_capped_and_truncation_detectable(settings: Settings) -> None:
    assert guard("SELECT wiki FROM edits", settings).fetch_limit == 101
    assert guard("SELECT wiki FROM edits LIMIT 5", settings).fetch_limit == 5
    assert guard("SELECT wiki FROM edits LIMIT 100000", settings).fetch_limit == 101
    assert "LIMIT 101" in guard("SELECT wiki FROM edits UNION ALL SELECT wiki FROM edits",
                                settings).physical_sql


def test_logical_sql_hides_policy_internals(settings: Settings) -> None:
    query = guard("SELECT editor FROM edits", make_settings(), role="enwiki_only")
    assert SALT not in query.logical_sql and "enwiki" not in query.logical_sql
    assert SALT in query.physical_sql and "edits_raw" in query.physical_sql
    assert query.datasets == ("edits",)


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery"])
def test_rewrite_renders_in_every_dialect(settings: Settings, dialect: str) -> None:
    query = guard("SELECT editor, count(*) FROM edits GROUP BY 1", settings, dialect=dialect)
    assert "MD5" in query.physical_sql.upper()


# --- Rejected before reaching the warehouse ------------------------------------------------

@pytest.mark.parametrize("sql", [
    "DROP TABLE edits",
    "DELETE FROM edits",
    "UPDATE edits SET wiki = 'x'",
    "INSERT INTO edits VALUES (1)",
    "CREATE TABLE x AS SELECT * FROM edits",
    "SELECT * FROM edits; DROP TABLE edits",
    "ATTACH 'other.db' AS other",
    "COPY edits TO 'out.csv'",
    "PRAGMA database_list",
    "SET threads = 1",
    "SELECT 1 INTO x FROM edits",
    "SELECT * FROM edits FOR UPDATE",
    "EXPLAIN SELECT * FROM edits",
])
def test_writes_and_side_effects_are_rejected(settings: Settings, sql: str) -> None:
    with pytest.raises(QueryRejected):
        guard(sql, settings, dialect="postgres" if "FOR UPDATE" in sql else "duckdb")


@pytest.mark.parametrize(("sql", "dialect"), [
    ("SELECT * FROM read_csv('/etc/passwd')", "duckdb"),
    ("SELECT * FROM '/etc/passwd'", "duckdb"),
    ("SELECT read_text('/etc/passwd') FROM edits", "duckdb"),
    ("SELECT getenv('HOME') FROM edits", "duckdb"),
    ("SELECT * FROM duckdb_settings()", "duckdb"),
    ("SELECT * FROM information_schema.tables", "duckdb"),
    ("SELECT * FROM main.edits_raw", "duckdb"),
    ("SELECT pg_read_file('/etc/passwd') FROM edits", "postgres"),
    ("SELECT pg_sleep(30) FROM edits", "postgres"),
    ("SELECT * FROM generate_series(1, 1000000000)", "postgres"),
    ("SELECT current_user FROM edits", "postgres"),
    ("SELECT * FROM pg_catalog.pg_user", "postgres"),
    ("SELECT * FROM EXTERNAL_QUERY('conn', 'select 1')", "bigquery"),
    ("SELECT * FROM ds.INFORMATION_SCHEMA.TABLES", "bigquery"),
])
def test_files_catalogs_and_session_functions_are_rejected(settings: Settings, sql: str,
                                                           dialect: str) -> None:
    with pytest.raises(QueryRejected):
        guard(sql, settings, dialect=dialect)


def test_ungranted_dataset_is_rejected_with_the_available_list(settings: Settings) -> None:
    with pytest.raises(QueryRejected, match=r"may not read salaries\. Available: edits"):
        guard("SELECT * FROM salaries", settings)


@pytest.mark.parametrize("sql", [
    # A physical table aliased to a CTE's name must not pass as the CTE (found in review).
    "WITH x AS (SELECT wiki FROM edits) SELECT editor FROM edits_raw x, x",
    "WITH x AS (SELECT wiki FROM edits) SELECT x.editor FROM edits x JOIN x y ON true",
    "WITH x AS (SELECT wiki FROM edits) SELECT * FROM salaries AS x",
])
def test_aliasing_a_table_to_a_cte_name_is_rejected(settings: Settings, sql: str) -> None:
    with pytest.raises(QueryRejected):
        guard(sql, settings)


def test_cte_references_by_their_own_name_still_work(demo_db: Path) -> None:
    rows = run("WITH a AS (SELECT editor, wiki FROM edits) "
               "SELECT count(DISTINCT b.editor) FROM a AS b JOIN a ON a.wiki = b.wiki", demo_db)
    assert rows == [(3,)]


def test_cte_shadowing_a_table_does_not_bypass_the_guard(settings: Settings) -> None:
    # Inside the CTE body, `salaries` is the real table, not the CTE.
    with pytest.raises(QueryRejected, match="may not read salaries"):
        guard("WITH salaries AS (SELECT * FROM salaries) SELECT * FROM salaries", settings)


@pytest.mark.parametrize("sql", [
    "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT * FROM r",
    "SELECT 1",
    "SELECT wiki FROM edits LIMIT (SELECT 5)",
    "SELEC wiki FROM edits",
])
def test_unbounded_unparseable_or_dataless_queries_are_rejected(settings: Settings,
                                                                 sql: str) -> None:
    with pytest.raises(QueryRejected):
        guard(sql, settings)


def test_undeclared_columns_do_not_exist(demo_db: Path) -> None:
    # Policy subqueries project declared columns only; the backend reports the unknown column.
    with pytest.raises(duckdb.BinderException):
        run("SELECT secret_column FROM edits", demo_db)
