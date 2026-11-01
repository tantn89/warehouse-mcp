"""End to end over the MCP protocol: an in-memory client calls the tools on the demo data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from warehouse_mcp.audit import AuditLog
from warehouse_mcp.backends import open_backend
from warehouse_mcp.config import load_settings
from warehouse_mcp.demo import build_demo_database, demo_config_path
from warehouse_mcp.server import build_server
from warehouse_mcp.service import WarehouseService

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def demo_service(tmp_path_factory: pytest.TempPathFactory) -> WarehouseService:
    db = build_demo_database(tmp_path_factory.mktemp("demo") / "wikipedia.duckdb")
    import os

    os.environ["WAREHOUSE_MCP_DEMO_DB"] = str(db)
    settings = load_settings(demo_config_path())
    audit_path = tmp_path_factory.mktemp("audit") / "audit.jsonl"
    service = WarehouseService(settings, open_backend(settings.connection),
                               AuditLog(str(audit_path)), pii_salt="testsalt0123456789")
    service.validate_against_warehouse()
    return service


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def call(service: WarehouseService, tool: str, args: dict[str, Any] | None = None,
               ) -> tuple[bool, Any]:
    async with Client(build_server(service)) as client:
        result = await client.call_tool(tool, args or {})
    if result.is_error:
        return True, result.content[0].text  # type: ignore[union-attr]
    return False, result.structured_content


async def test_tools_are_listed_read_only(demo_service: WarehouseService) -> None:
    async with Client(build_server(demo_service)) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {"warehouse_list_datasets", "warehouse_describe_dataset",
                                       "warehouse_query_metrics", "warehouse_run_sql"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)
    assert all(t.output_schema for t in tools)


async def test_list_and_describe(demo_service: WarehouseService) -> None:
    error, listing = await call(demo_service, "warehouse_list_datasets")
    assert not error and "edits" in {d["name"] for d in listing["datasets"]}
    error, detail = await call(demo_service, "warehouse_describe_dataset", {"dataset": "edits"})
    assert not error
    editor = next(c for c in detail["columns"] if c["name"] == "editor")
    assert editor["pseudonymized"] and editor["type"] != "unknown"
    assert all(str(r["editor"]).startswith("pii_") for r in detail["sample_rows"] if r["editor"])


async def test_query_metrics_matches_hand_written_sql(demo_service: WarehouseService) -> None:
    error, metrics = await call(demo_service, "warehouse_query_metrics", {
        "dataset": "edits", "metrics": ["edits", "editors"], "dimensions": ["wiki"],
        "filters": [{"column": "is_bot", "op": "=", "value": False}],
        "order_by": "edits", "limit": 5})
    assert not error, metrics
    error, sql = await call(demo_service, "warehouse_run_sql", {"sql": """
        SELECT wiki, count(*) AS edits, count(DISTINCT editor) AS editors FROM edits
        WHERE NOT is_bot GROUP BY wiki ORDER BY edits DESC LIMIT 5"""})
    assert not error
    assert metrics["rows"] == sql["rows"]


async def test_time_grain_and_range(demo_service: WarehouseService) -> None:
    error, out = await call(demo_service, "warehouse_query_metrics", {
        "dataset": "edits", "metrics": ["edits"], "time_grain": "minute",
        "since": "2000-01-01T00:00:00Z", "order_by": "period", "descending": False,
        "limit": 3})
    assert not error, out
    assert out["columns"] == ["period", "edits"] and out["row_count"] == 3


async def test_rejections_are_tool_errors_with_a_next_step(
        demo_service: WarehouseService) -> None:
    error, message = await call(demo_service, "warehouse_run_sql",
                                {"sql": "DROP TABLE edits"})
    assert error and "Only read queries" in message
    error, message = await call(demo_service, "warehouse_query_metrics",
                                {"dataset": "edits", "metrics": ["revenue"]})
    assert error and "Available:" in message


async def test_every_call_is_audited_without_result_values(
        demo_service: WarehouseService) -> None:
    await call(demo_service, "warehouse_run_sql", {"sql": "SELECT editor FROM edits LIMIT 1"})
    await call(demo_service, "warehouse_run_sql", {"sql": "SELECT * FROM salaries"})
    audit_file = Path(demo_service.audit._path)  # type: ignore[arg-type]
    events = [json.loads(line) for line in audit_file.read_text().splitlines()]
    decisions = {e["decision"] for e in events}
    assert {"allowed", "denied"} <= decisions
    text = audit_file.read_text()
    assert "testsalt0123456789" not in text and "pii_" not in text
