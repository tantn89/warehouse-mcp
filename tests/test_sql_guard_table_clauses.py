"""Clauses on a dataset reference that the policy rewrite could not carry over are refused.

The rewrite replaces each dataset's table node with the role's policy subquery. Before, any
clause attached to that node (SAMPLE, PIVOT, a parenthesized join, a column alias list,
time travel, PARTITION) vanished and the agent got a different answer with no error.
"""

from __future__ import annotations

import pytest

from conftest import SALT
from warehouse_mcp.config import Settings
from warehouse_mcp.sql_guard import QueryRejected, guard_query

CLAUSES = [
    ("SELECT * FROM edits SAMPLE (10)", "snowflake", "SAMPLE"),
    ("SELECT * FROM edits TABLESAMPLE BERNOULLI (10)", "snowflake", "SAMPLE"),
    ("SELECT * FROM edits SAMPLE ROW (5 ROWS)", "snowflake", "SAMPLE"),
    ("SELECT * FROM edits TABLESAMPLE 10%", "duckdb", "SAMPLE"),
    ("SELECT * FROM edits TABLESAMPLE SYSTEM (10)", "postgres", "SAMPLE"),
    ("SELECT * FROM edits TABLESAMPLE SYSTEM (10 PERCENT)", "bigquery", "SAMPLE"),
    ("SELECT * FROM edits PIVOT (count(event_id) FOR wiki IN ('enwiki', 'dewiki'))",
     "snowflake", "PIVOT"),
    ("SELECT * FROM edits UNPIVOT (v FOR k IN (wiki, comment))", "snowflake", "PIVOT"),
    ("SELECT * FROM edits PIVOT (count(*) FOR wiki IN ('enwiki'))", "duckdb", "PIVOT"),
    ("SELECT * FROM edits PIVOT (count(*) FOR wiki IN ('enwiki'))", "bigquery", "PIVOT"),
    ("SELECT * FROM (edits JOIN edits AS f ON true)", "snowflake", "join"),
    ("SELECT * FROM (edits JOIN edits AS f ON true)", "duckdb", "join"),
    ("SELECT * FROM (edits JOIN edits AS f ON true)", "postgres", "join"),
    ("SELECT a, b FROM edits AS e(a, b)", "snowflake", "Column aliases"),
    ("SELECT a FROM edits AS e(a, b)", "duckdb", "Column aliases"),
    ("SELECT a FROM edits AS e(a, b)", "postgres", "Column aliases"),
    ("SELECT * FROM edits PARTITION (p1)", "postgres", "Column aliases"),  # parsed as e(p1)
    ("SELECT * FROM ONLY edits", "postgres", "ONLY"),
    ("SELECT * FROM edits AT(OFFSET => -60)", "snowflake", "Time travel"),
    ("SELECT * FROM edits FOR SYSTEM_TIME AS OF '2026-01-01'", "bigquery", "Time travel"),
    ("SELECT * FROM edits FOR SYSTEM_TIME AS OF '2026-01-01'", "duckdb", "Time travel"),
]


@pytest.mark.parametrize(("sql", "dialect", "message"), CLAUSES)
def test_table_clauses_the_rewrite_would_drop_are_refused(settings: Settings, sql: str,
                                                          dialect: str, message: str) -> None:
    with pytest.raises(QueryRejected, match=message):
        guard_query(sql, settings, settings.role("enwiki_only"), dialect, SALT)


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "snowflake", "bigquery"])
def test_the_same_clauses_on_a_subquery_are_kept(settings: Settings, dialect: str) -> None:
    sql = "SELECT * FROM (SELECT wiki FROM edits) AS t TABLESAMPLE BERNOULLI (10)"
    if dialect in ("duckdb", "bigquery"):
        sql = "SELECT wiki, count(*) FROM (SELECT wiki FROM edits) AS t GROUP BY wiki"
    query = guard_query(sql, settings, settings.role("enwiki_only"), dialect, SALT)
    assert "enwiki" in query.physical_sql


@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM edits, LATERAL SPLIT_TO_TABLE(REPEAT('a,', 10000000), ',') s",
    "SELECT count(*) FROM edits, LATERAL STRTOK_SPLIT_TO_TABLE(wiki, ' ') s",
    "SELECT count(*) FROM edits, TABLE(SPLIT_TO_TABLE('a,b', ','))",
    "SELECT count(*) FROM edits, LATERAL FLATTEN(INPUT => PARSE_JSON('[1]')) f",
])
def test_row_generating_table_functions_are_refused_on_snowflake(settings: Settings,
                                                                 sql: str) -> None:
    with pytest.raises(QueryRejected):
        guard_query(sql, settings, settings.role("analyst"), "snowflake", SALT)


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "snowflake"])
def test_lateral_subqueries_still_work(settings: Settings, dialect: str) -> None:
    query = guard_query("SELECT e.wiki, l.n FROM edits AS e, LATERAL (SELECT 1 AS n) AS l",
                        settings, settings.role("enwiki_only"), dialect, SALT)
    assert "enwiki" in query.physical_sql
