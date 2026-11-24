"""The Postgres parity cases, run against Snowflake, and the engine's own refusals.

Needs the objects from docker/snowflake-setup.sql, the snapshot loaded with
scripts/load_snapshot.py --target snowflake, and the reader's key, so it is skipped unless both
WAREHOUSE_MCP_SNOWFLAKE_ACCOUNT and WAREHOUSE_MCP_SNOWFLAKE_KEY_FILE are set:

    WAREHOUSE_MCP_SNOWFLAKE_ACCOUNT=<orgname-accountname> \
    WAREHOUSE_MCP_SNOWFLAKE_KEY_FILE=<reader key path> pytest -s tests/test_snowflake_parity.py

WAREHOUSE_MCP_SNOWFLAKE_TABLES overrides where the tables live (default WAREHOUSE_MCP.WIKIPEDIA).
Also exports the service builder test_red_team.py uses for its Snowflake run.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from test_postgres_parity import CASES, _environment, _service, duckdb_demo_service, normalized
from warehouse_mcp.backends import BackendError
from warehouse_mcp.config import Limits, load_settings
from warehouse_mcp.service import QueryOutput, WarehouseService

SNOWFLAKE_ENABLED = bool(os.environ.get("WAREHOUSE_MCP_SNOWFLAKE_ACCOUNT")
                         and os.environ.get("WAREHOUSE_MCP_SNOWFLAKE_KEY_FILE"))
TABLES = os.environ.get("WAREHOUSE_MCP_SNOWFLAKE_TABLES", "WAREHOUSE_MCP.WIKIPEDIA")
SNOWFLAKE_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "snowflake.yaml"

pytestmark = pytest.mark.skipif(not SNOWFLAKE_ENABLED,
                                reason="set WAREHOUSE_MCP_SNOWFLAKE_ACCOUNT and _KEY_FILE")


def snowflake_demo_service(directory: Path, limits: Limits | None = None) -> WarehouseService:
    # As for Postgres: the base demo config references WAREHOUSE_MCP_DEMO_DB, so it must exist.
    # Scoped: a leaked WAREHOUSE_MCP_SCHEMA would point later DuckDB tests at Snowflake names.
    demo_db = os.environ.get("WAREHOUSE_MCP_DEMO_DB", "unused-by-snowflake")
    with _environment(WAREHOUSE_MCP_DEMO_DB=demo_db, WAREHOUSE_MCP_SCHEMA=TABLES):
        return _service(load_settings(SNOWFLAKE_CONFIG), directory, limits)


@pytest.fixture(scope="module")
def engines(tmp_path_factory: pytest.TempPathFactory,
            ) -> Iterator[tuple[WarehouseService, WarehouseService]]:
    directory = tmp_path_factory.mktemp("snowflake")
    duck, sf = duckdb_demo_service(directory), snowflake_demo_service(directory)
    yield duck, sf
    duck.backend.close()
    sf.backend.close()


@pytest.mark.parametrize("case", list(CASES))
def test_duckdb_and_snowflake_agree(engines: tuple[WarehouseService, WarehouseService],
                                    case: str) -> None:
    duck, sf = (CASES[case](service) for service in engines)
    assert duck.row_count > 0
    print(json.dumps({"case": case, "elapsed_ms": sf.elapsed_ms}))
    assert at_snowflake_scale(duck) == at_snowflake_scale(sf)


def at_snowflake_scale(output: QueryOutput) -> tuple[list[str], list[str], bool]:
    """Snowflake computes AVG and division over integers as fixed-point NUMBER with 6 decimal
    places, where DuckDB returns DOUBLE. Both sides are rounded half away from zero, as
    Snowflake rounds, to 6 decimals; everything else must match exactly."""
    def cell(value: object) -> object:
        if isinstance(value, float):
            return float(Decimal(repr(value)).quantize(Decimal("0.000001"), ROUND_HALF_UP))
        return value

    columns, rows, truncated = normalized(output)
    scaled = sorted(json.dumps([cell(v) for v in json.loads(row)]) for row in rows)
    return columns, scaled, truncated


# --- The Snowflake engine refuses on its own, with the guard out of the way -------------

@pytest.mark.parametrize("statement", [
    f"INSERT INTO {TABLES}.EDITS (EVENT_ID) VALUES ('injected')",
    f"UPDATE {TABLES}.EDITS SET WIKI = 'x'",
    f"DELETE FROM {TABLES}.EDITS",
    f"DROP TABLE {TABLES}.EDITS",
    f"CREATE TABLE {TABLES}.PWNED (X INT)",
    f"CREATE STAGE {TABLES}.PWNED",
    "CREATE DATABASE PWNED",
])
def test_reader_grants_refuse_writes(engines: tuple[WarehouseService, WarehouseService],
                                     statement: str) -> None:
    # Straight through the connector as MCP_READER: only the grants are left to refuse.
    with pytest.raises(BackendError, match=r"(?i)insufficient privileges"):
        engines[1].backend.execute(statement, 1, 10)


def test_unloading_to_the_user_stage_is_switched_off(
        engines: tuple[WarehouseService, WarehouseService]) -> None:
    # The user stage needs no grant; PREVENT_UNLOAD_TO_INTERNAL_STAGES on the user closes it.
    with pytest.raises(BackendError, match=r"(?i)unload|internal stage"):
        engines[1].backend.execute(f"COPY INTO @~/dump FROM {TABLES}.EDITS", 1, 10)


# --- What the account's PUBLIC role gives every role: recorded, not asserted away ------

def test_public_grants_are_visible_to_the_reader(
        engines: tuple[WarehouseService, WarehouseService]) -> None:
    # Grants cannot hide PUBLIC's shares (often SNOWFLAKE_SAMPLE_DATA on trial accounts); the
    # guard is what keeps them out of reach of agents. Printed for docs/snowflake-run.md.
    databases = engines[1].backend.execute("SHOW DATABASES", 100, 10)
    print(json.dumps({"databases_visible_to_reader": [str(row[1]) for row in databases.rows]}))
    try:
        sample = engines[1].backend.execute(
            "SELECT count(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.NATION", 1, 10)
        print(json.dumps({"sample_data_readable": True, "rows": sample.rows[0][0]}))
    except BackendError as exc:
        print(json.dumps({"sample_data_readable": False, "error": str(exc)}))


def test_statement_timeout_cancels_on_the_server(
        engines: tuple[WarehouseService, WarehouseService]) -> None:
    started = time.perf_counter()
    with pytest.raises(BackendError, match="limit"):
        engines[1].backend.execute("SELECT SYSTEM$WAIT(20)", 1, 2)
    assert time.perf_counter() - started < 15
