"""The Postgres parity cases, run against BigQuery: same answers as DuckDB, bytes recorded.

Needs a loaded dataset and Application Default Credentials, so it is skipped unless both
WAREHOUSE_MCP_BIGQUERY_PROJECT and WAREHOUSE_MCP_BIGQUERY_DATASET are set:

    python scripts/load_snapshot.py --target bigquery --project P --dataset D --location US
    WAREHOUSE_MCP_BIGQUERY_PROJECT=P WAREHOUSE_MCP_BIGQUERY_DATASET=D pytest -s \
        tests/test_bigquery_parity.py
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from test_postgres_parity import CASES, SALT, _environment, duckdb_demo_service, normalized
from warehouse_mcp.audit import AuditLog
from warehouse_mcp.backends import open_backend
from warehouse_mcp.config import load_settings
from warehouse_mcp.service import QueryOutput, WarehouseService

PROJECT = os.environ.get("WAREHOUSE_MCP_BIGQUERY_PROJECT")
DATASET = os.environ.get("WAREHOUSE_MCP_BIGQUERY_DATASET")
BIGQUERY_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "bigquery.yaml"

pytestmark = pytest.mark.skipif(not (PROJECT and DATASET),
                                reason="set WAREHOUSE_MCP_BIGQUERY_PROJECT and _DATASET")


@pytest.fixture(scope="module")
def engines(tmp_path_factory: pytest.TempPathFactory,
            ) -> Iterator[tuple[WarehouseService, WarehouseService]]:
    directory = tmp_path_factory.mktemp("bigquery")
    duck = duckdb_demo_service(directory)
    # Scoped: a leaked WAREHOUSE_MCP_SCHEMA would point later DuckDB tests at BigQuery names.
    with _environment(GOOGLE_CLOUD_PROJECT=str(PROJECT),
                      WAREHOUSE_MCP_SCHEMA=f"{PROJECT}.{DATASET}"):
        settings = load_settings(BIGQUERY_CONFIG)
    bq = WarehouseService(settings, open_backend(settings.connection),
                          AuditLog(str(directory / "audit-bigquery.jsonl")), pii_salt=SALT)
    bq.validate_against_warehouse()
    yield duck, bq
    duck.backend.close()
    bq.backend.close()


def utc_naive(output: QueryOutput) -> Any:
    """BigQuery TIMESTAMPs come back timezone-aware; the snapshot's times are naive UTC."""
    columns, rows, truncated = normalized(output)
    return columns, sorted(row.replace("+00:00", "") for row in rows), truncated


@pytest.mark.parametrize("case", list(CASES))
def test_duckdb_and_bigquery_agree(engines: tuple[WarehouseService, WarehouseService],
                                   case: str) -> None:
    duck, bq = (CASES[case](service) for service in engines)
    assert duck.row_count > 0
    print(json.dumps({"case": case, "bytes_processed": bq.bytes_processed,
                      "elapsed_ms": bq.elapsed_ms}))
    assert utc_naive(duck) == utc_naive(bq)
