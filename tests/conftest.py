"""Shared fixtures: a small semantic model with a PII column, a restricted role and a DuckDB
database holding the same tables, so guard rewrites can be executed, not just inspected."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from warehouse_mcp.config import Settings

SALT = "0123456789abcdef"


def make_settings(db_path: str = ":memory:", **overrides: object) -> Settings:
    raw: dict[str, object] = {
        "connection": {"kind": "duckdb", "path": db_path},
        "datasets": [
            {
                "name": "edits", "table": "main.edits_raw", "description": "One row per edit.",
                "grain": "edit", "time_column": "event_time",
                "columns": [
                    {"name": "event_id"}, {"name": "event_time"}, {"name": "wiki"},
                    {"name": "editor", "pii": True}, {"name": "is_bot"},
                    {"name": "comment", "free_text": True},
                ],
                "metrics": [{"name": "edits", "sql": "count(*)"},
                            {"name": "editors", "sql": "count(DISTINCT editor)"}],
            },
            {
                "name": "salaries", "table": "main.salaries", "description": "Restricted.",
                "grain": "employee", "columns": [{"name": "employee"}, {"name": "salary"}],
            },
        ],
        "roles": [
            {"name": "analyst", "datasets": ["edits"]},
            {"name": "enwiki_only", "datasets": ["edits"],
             "row_filters": {"edits": "wiki = 'enwiki'"}},
            {"name": "admin", "datasets": "*", "pii_clearance": True},
        ],
        "default_role": "analyst",
        "limits": {"max_rows": 100},
    }
    raw.update(overrides)
    return Settings.model_validate(raw)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def demo_db(tmp_path: Path) -> Path:
    path = tmp_path / "demo.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE edits_raw AS SELECT * FROM (VALUES
          ('e1', TIMESTAMP '2026-11-01 10:00:00', 'enwiki', 'Alice', false, 'fix typo'),
          ('e2', TIMESTAMP '2026-11-01 10:01:00', 'enwiki', 'Alice', false, 'mail me a@b.com'),
          ('e3', TIMESTAMP '2026-11-01 10:02:00', 'dewiki', 'Bob', false, 'call +1 415 555 0100'),
          ('e4', TIMESTAMP '2026-11-01 10:03:00', 'enwiki', 'CleanupBot', true, NULL),
          ('e5', TIMESTAMP '2026-11-01 10:04:00', 'frwiki', NULL, false, 'anon')
        ) t(event_id, event_time, wiki, editor, is_bot, comment)""")
    con.execute("CREATE TABLE salaries AS SELECT 'Alice' AS employee, 100 AS salary")
    con.close()
    return path
