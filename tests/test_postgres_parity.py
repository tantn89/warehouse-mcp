"""DuckDB and Postgres must answer the same governed questions identically.

The bundled snapshot is loaded into both engines (scripts/load_snapshot.py for Postgres), the
same semantic model and salt serve both, and each case runs through WarehouseService, so the
SQL guard, pseudonyms and row filters are part of what is compared. A second group calls the
Postgres backend directly to show the engine refuses writes and long queries on its own.

Needs a loaded Postgres: docker compose up -d --wait && python scripts/load_snapshot.py, then
WAREHOUSE_MCP_POSTGRES_DSN=postgresql://mcp_reader:...@127.0.0.1:55432/warehouse.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from warehouse_mcp.audit import AuditLog
from warehouse_mcp.backends import BackendError, open_backend
from warehouse_mcp.config import Limits, Settings, load_settings
from warehouse_mcp.demo import build_demo_database
from warehouse_mcp.semantic import Filter
from warehouse_mcp.service import QueryOutput, WarehouseService

POSTGRES_DSN = os.environ.get("WAREHOUSE_MCP_POSTGRES_DSN")
SALT = "parity0123456789salt"  # one salt for both engines: pseudonyms must match exactly
DEMO_CONFIG = Path(str(resources.files("warehouse_mcp.demo") / "wikipedia.yaml"))
POSTGRES_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "postgres.yaml"

pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN, reason="set WAREHOUSE_MCP_POSTGRES_DSN to a loaded Postgres (see module doc)")

Case = Callable[[WarehouseService], QueryOutput]


# --- Service builders, shared with test_red_team.py ------------------------------------

@contextmanager
def _environment(**values: str) -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _service(settings: Settings, directory: Path, limits: Limits | None) -> WarehouseService:
    if limits:
        settings = settings.model_copy(update={"limits": limits})
    service = WarehouseService(settings, open_backend(settings.connection),
                               AuditLog(str(directory / f"audit-{settings.connection.kind}.jsonl")),
                               pii_salt=SALT)
    service.validate_against_warehouse()
    return service


def duckdb_demo_service(directory: Path, limits: Limits | None = None) -> WarehouseService:
    database = build_demo_database(directory / "wikipedia.duckdb")
    with _environment(WAREHOUSE_MCP_DEMO_DB=str(database), WAREHOUSE_MCP_SCHEMA="main"):
        return _service(load_settings(DEMO_CONFIG), directory, limits)


def postgres_demo_service(directory: Path, limits: Limits | None = None) -> WarehouseService:
    # The base demo config references WAREHOUSE_MCP_DEMO_DB, and `extends` expands the base
    # before `connection` replaces it, so the variable must exist even though Postgres
    # never reads it.
    demo_db = os.environ.get("WAREHOUSE_MCP_DEMO_DB", "unused-by-postgres")
    with _environment(WAREHOUSE_MCP_DEMO_DB=demo_db, WAREHOUSE_MCP_SCHEMA="wikipedia"):
        return _service(load_settings(POSTGRES_CONFIG), directory, limits)


@pytest.fixture(scope="module")
def engines(tmp_path_factory: pytest.TempPathFactory,
            ) -> Iterator[tuple[WarehouseService, WarehouseService]]:
    directory = tmp_path_factory.mktemp("parity")
    duck, pg = duckdb_demo_service(directory), postgres_demo_service(directory)
    yield duck, pg
    duck.backend.close()
    pg.backend.close()


def sql(role: str, text: str) -> Case:
    return lambda svc: svc.run_sql(svc.settings.role(role), text)


def metrics(role: str, dataset: str, names: list[str], dimensions: list[str] | None = None,
            filters: list[Filter] | None = None, time_grain: Any = None,
            since: datetime | None = None, until: datetime | None = None,
            order_by: str | None = None, descending: bool = True, limit: int = 100) -> Case:
    return lambda svc: svc.query_metrics(svc.settings.role(role), dataset, names,
                                         dimensions or [], filters or [], time_grain, since,
                                         until, order_by, descending, limit)


WINDOW = (datetime(2026, 11, 1, 4, 30), datetime(2026, 11, 1, 4, 50))
CASES: dict[str, Case] = {
    "sql_edits_per_wiki": sql("analyst", """
        SELECT wiki, count(*) AS edits FROM edits GROUP BY wiki
        ORDER BY edits DESC, wiki LIMIT 25"""),
    "sql_distinct_pseudonymized_human_editors": sql("analyst", """
        SELECT wiki, count(DISTINCT editor) AS editors FROM edits WHERE NOT is_bot
        GROUP BY wiki ORDER BY editors DESC, wiki LIMIT 25"""),
    "sql_top_editors_as_pseudonyms": sql("analyst", """
        SELECT editor, count(*) AS n FROM edits WHERE editor IS NOT NULL
        GROUP BY editor ORDER BY n DESC, editor LIMIT 15"""),
    "sql_join_edits_to_their_sessions": sql("analyst", """
        SELECT s.is_bot, count(DISTINCT s.editor) AS editors, count(*) AS edits_in_sessions
        FROM edits e JOIN editor_sessions s ON e.editor = s.editor AND e.wiki = s.wiki
         AND e.event_time BETWEEN s.session_start AND s.session_end
        GROUP BY s.is_bot ORDER BY s.is_bot"""),
    "sql_net_bytes_per_namespace": sql("analyst", """
        SELECT namespace, sum(length_delta) AS net, avg(length_delta) AS mean
        FROM edits GROUP BY namespace ORDER BY namespace"""),
    "sql_marts_dates_and_doubles": sql("analyst", """
        SELECT event_date, wiki, edits, bot_share FROM bot_activity_daily
        ORDER BY edits DESC, wiki LIMIT 10"""),
    "sql_trending_timestamps": sql("analyst", """
        SELECT wiki, title, last_edit_at, trend_rank FROM trending_articles
        ORDER BY trend_rank, title"""),
    "metrics_by_wiki_with_filters": metrics(
        "analyst", "edits", ["edits", "editors", "bytes_added", "bytes_removed"], ["wiki"],
        [Filter(column="is_bot", op="=", value=False),
         Filter(column="event_type", op="in", value=["edit", "new"])],
        order_by="edits", limit=10),
    "metrics_minute_grain_in_window": metrics(
        "analyst", "edits", ["edits", "new_pages"], time_grain="minute", since=WINDOW[0],
        until=WINDOW[1], order_by="period", descending=False),
    "metrics_hour_grain_by_event_type": metrics(
        "analyst", "edits", ["edits", "bot_share"], ["event_type"], time_grain="hour"),
    "metrics_day_grain_mart_filtered": metrics(
        "analyst", "edits_per_minute", ["total_edits", "total_bot_edits"], ["wiki"],
        [Filter(column="wiki", op="in", value=["enwiki", "dewiki", "wikidatawiki"])],
        time_grain="day"),
    "metrics_sessions_by_bot_flag": metrics(
        "analyst", "editor_sessions", ["sessions", "avg_edits_per_session"], ["is_bot"]),
    "partner_metrics_only_enwiki": metrics(
        "enwiki_partner", "edits", ["edits", "editors"], ["wiki"]),
    "partner_sql_joins_within_filter": sql("enwiki_partner", """
        SELECT m.wiki, count(*) AS minutes, sum(m.edits) AS edits
        FROM edits_per_minute m JOIN (SELECT DISTINCT wiki FROM edits) e ON e.wiki = m.wiki
        GROUP BY m.wiki"""),
    "cleared_role_sees_names": sql("trust_and_safety", """
        SELECT editor, count(*) AS n FROM edits WHERE is_bot GROUP BY editor
        ORDER BY n DESC, editor LIMIT 5"""),
}


def normalized(output: QueryOutput) -> tuple[list[str], list[str], bool]:
    """Columns, rows as sorted JSON and truncation. Numbers compare at 10 significant digits:
    Postgres returns numeric (Decimal) for sum/avg where DuckDB returns HUGEINT or DOUBLE."""
    def cell(value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(f"{float(value):.10g}")
        return value

    rows = sorted(json.dumps([cell(v) for v in row]) for row in output.rows)
    return output.columns, rows, output.truncated


@pytest.mark.parametrize("case", list(CASES))
def test_duckdb_and_postgres_agree(engines: tuple[WarehouseService, WarehouseService],
                                   case: str) -> None:
    duck, pg = (CASES[case](service) for service in engines)
    assert duck.row_count > 0, "a parity case must return rows to mean anything"
    assert normalized(duck) == normalized(pg)


def test_partner_rows_stay_inside_the_filter(
        engines: tuple[WarehouseService, WarehouseService]) -> None:
    for service in engines:
        out = CASES["partner_metrics_only_enwiki"](service)
        assert [row[0] for row in out.rows] == ["enwiki"]


# --- The Postgres engine refuses on its own, with the guard out of the way --------------

@pytest.mark.parametrize("statement", [
    "INSERT INTO wikipedia.edits (event_id) VALUES ('injected')",
    "UPDATE wikipedia.edits SET wiki = 'x'",
    "DELETE FROM wikipedia.edits",
    "CREATE TABLE wikipedia.pwned (x int)",
    "CREATE TEMP TABLE pwned (x int)",
    "DROP TABLE wikipedia.edits",
    "COPY (SELECT 1) TO PROGRAM 'id'",
    "SELECT pg_read_file('/etc/passwd')",
])
def test_backend_refuses_writes_and_server_files(
        engines: tuple[WarehouseService, WarehouseService], statement: str) -> None:
    with pytest.raises(BackendError):
        engines[1].backend.execute(statement, 1, 5)


@pytest.mark.parametrize("statement", [
    "INSERT INTO wikipedia.edits (event_id) VALUES ('injected')",
    "CREATE TABLE wikipedia.pwned (x int)",
    "CREATE TABLE public.pwned (x int)",
    "CREATE TEMP TABLE pwned (x int)",
])
def test_reader_grants_refuse_writes_in_a_read_write_session(statement: str) -> None:
    # Bypass the backend's read-only transaction too: only the grants are left to refuse.
    import psycopg

    assert POSTGRES_DSN
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement.encode())


def test_statement_timeout_cancels_on_the_server(
        engines: tuple[WarehouseService, WarehouseService]) -> None:
    started = time.perf_counter()
    with pytest.raises(BackendError, match="limit"):
        engines[1].backend.execute("SELECT pg_sleep(10)", 1, 0.5)
    assert time.perf_counter() - started < 5


def test_audit_log_never_holds_the_salt(
        engines: tuple[WarehouseService, WarehouseService]) -> None:
    for service in engines:
        CASES["sql_top_editors_as_pseudonyms"](service)
        assert SALT not in Path(str(service.audit._path)).read_text(encoding="utf-8")
