"""Transport-independent core: every tool call goes role -> guard -> backend -> redact -> audit.

The MCP layer only maps tools onto these methods. Keeping the policy path here means stdio,
HTTP and the tests all exercise exactly the same code.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from warehouse_mcp.audit import AuditLog
from warehouse_mcp.backends import Backend, BackendError
from warehouse_mcp.config import Dataset, Role, Settings
from warehouse_mcp.redaction import redact
from warehouse_mcp.semantic import Filter, TimeGrain, compile_metrics_query
from warehouse_mcp.sql_guard import QueryRejected, guard_query


class ConfigMismatch(Exception):
    """The config declares columns the warehouse does not have."""


class MetricInfo(BaseModel):
    name: str
    description: str


class DatasetSummary(BaseModel):
    name: str
    description: str
    grain: str
    time_column: str | None
    metrics: list[MetricInfo]


class ColumnInfo(BaseModel):
    name: str
    type: str
    description: str
    pseudonymized: bool
    pseudonymized_when: str | None = None  # only rows matching this predicate are masked
    free_text: bool


class DatasetDetail(DatasetSummary):
    columns: list[ColumnInfo]
    row_policy: bool
    sample_rows: list[dict[str, Any]]


class QueryOutput(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    sql: str
    elapsed_ms: int
    bytes_processed: int | None = None
    redacted_values: int = 0


class WarehouseService:
    def __init__(self, settings: Settings, backend: Backend, audit: AuditLog,
                 pii_salt: str | None = None) -> None:
        if pii_salt is not None and not re.fullmatch(r"[A-Za-z0-9]{16,128}", pii_salt):
            raise ValueError("The PII salt must be 16-128 letters or digits.")
        self.settings = settings
        self.backend = backend
        self.audit = audit
        # A random salt per process when none is configured: pseudonyms stay consistent within
        # a session, and cannot be precomputed from a list of known usernames.
        self._salt = pii_salt or secrets.token_hex(16)
        self._column_types: dict[str, dict[str, str]] = {}

    def validate_against_warehouse(self) -> None:
        """Fail at startup, not mid-conversation, when the config and the warehouse disagree."""
        for dataset in self.settings.datasets:
            actual = self.backend.table_columns(dataset.table)
            missing = [c.name for c in dataset.columns if c.name not in actual]
            if not actual or missing:
                raise ConfigMismatch(f"{dataset.name} ({dataset.table}): missing columns "
                                     f"{missing or 'all: table not found'}")
            self._column_types[dataset.name] = actual

    # --- discovery -----------------------------------------------------------------------

    def list_datasets(self, role: Role) -> list[DatasetSummary]:
        summaries = [_summary(d) for d in self.settings.datasets if role.can_read(d.name)]
        self.audit.record(role=role.name, tool="list_datasets", decision="allowed",
                          rows=len(summaries))
        return summaries

    def describe_dataset(self, role: Role, name: str) -> DatasetDetail:
        dataset = self.settings.dataset(name)
        if dataset is None or not role.can_read(dataset.name):
            available = ", ".join(d.name for d in self.settings.datasets if role.can_read(d.name))
            self.audit.record(role=role.name, tool="describe_dataset", decision="denied",
                              dataset=name, reason="unknown or not granted")
            raise QueryRejected(f"No dataset {name!r} for role {role.name}. Available: "
                                f"{available}.")
        sample = self._execute(role, "describe_dataset",
                               f"SELECT * FROM {dataset.name} LIMIT 3", record=False)
        types = self._column_types.get(dataset.name, {})
        detail = DatasetDetail(
            **_summary(dataset).model_dump(),
            columns=[ColumnInfo(name=c.name, type=types.get(c.name, "unknown"),
                                description=c.description,
                                pseudonymized=c.pii and not role.pii_clearance,
                                pseudonymized_when=(c.pii_when if not role.pii_clearance
                                                    else None),
                                free_text=c.free_text) for c in dataset.columns],
            row_policy=dataset.name in role.row_filters,
            sample_rows=[dict(zip(sample.columns, row, strict=True)) for row in sample.rows],
        )
        self.audit.record(role=role.name, tool="describe_dataset", decision="allowed",
                          dataset=dataset.name)
        return detail

    # --- queries -------------------------------------------------------------------------

    def run_sql(self, role: Role, sql: str) -> QueryOutput:
        return self._execute(role, "run_sql", sql)

    def query_metrics(self, role: Role, dataset_name: str, metrics: list[str],
                      dimensions: list[str], filters: list[Filter],
                      time_grain: TimeGrain | None, since: datetime | None,
                      until: datetime | None, order_by: str | None, descending: bool,
                      limit: int) -> QueryOutput:
        dataset = self.settings.dataset(dataset_name)
        try:
            if dataset is None or not role.can_read(dataset.name):
                raise QueryRejected(f"No dataset {dataset_name!r} for role {role.name}.")
            sql = compile_metrics_query(dataset, metrics, dimensions, filters, time_grain,
                                        since, until, order_by, descending, limit,
                                        self.backend.dialect)
        except QueryRejected as exc:
            self.audit.record(role=role.name, tool="query_metrics", decision="denied",
                              reason=str(exc), dataset=dataset_name)
            raise
        return self._execute(role, "query_metrics", sql)

    def _execute(self, role: Role, tool: str, sql: str, record: bool = True) -> QueryOutput:
        limits = self.settings.limits
        try:
            guarded = guard_query(sql, self.settings, role, self.backend.dialect, self._salt)
        except QueryRejected as exc:
            self.audit.record(role=role.name, tool=tool, decision="denied", sql=sql,
                              reason=str(exc))
            raise
        started = time.perf_counter()
        try:
            result = self.backend.execute(guarded.physical_sql, guarded.fetch_limit,
                                          limits.timeout_s)
        except BackendError as exc:
            self.audit.record(role=role.name, tool=tool, decision="error",
                              sql=guarded.logical_sql, reason=str(exc))
            raise
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        free_text = self._free_text_columns(guarded.datasets)
        rows, redacted, truncated_cells = [], 0, False
        for raw in result.rows:
            row = []
            for column, value in zip(result.columns, raw, strict=True):
                cell = _json_value(value)
                if isinstance(cell, str):
                    # Cut first: bounds both the payload and the time redaction can take.
                    if len(cell) > limits.max_cell_chars:
                        cell, truncated_cells = cell[:limits.max_cell_chars] + "…[cut]", True
                    cell, found = redact(cell, phones=column in free_text)
                    redacted += found
                row.append(cell)
            rows.append(row)
        truncated = len(rows) > limits.max_rows or truncated_cells
        rows = rows[:limits.max_rows]
        while len(rows) > 1 and len(json.dumps(rows, default=str)) > limits.max_result_chars:
            rows, truncated = rows[: len(rows) // 2], True
        if rows and _fit_single_row(rows[0], limits.max_result_chars):
            truncated = True

        output = QueryOutput(columns=result.columns, rows=rows, row_count=len(rows),
                             truncated=truncated, sql=guarded.logical_sql,
                             elapsed_ms=elapsed_ms, bytes_processed=result.bytes_processed,
                             redacted_values=redacted)
        if record:
            self.audit.record(role=role.name, tool=tool, decision="allowed",
                              sql=guarded.logical_sql, datasets=list(guarded.datasets),
                              rows=len(rows), truncated=truncated, elapsed_ms=elapsed_ms,
                              bytes_processed=result.bytes_processed, redacted=redacted)
        return output

    def _free_text_columns(self, dataset_names: tuple[str, ...]) -> set[str]:
        """Output column names that get phone redaction. Name-based: an alias such as
        `comment AS c` keeps email redaction (applied everywhere) but skips the phone pass."""
        names: set[str] = set()
        for name in dataset_names:
            dataset = self.settings.dataset(name)
            if dataset:
                names |= {c.name for c in dataset.columns if c.free_text}
        return names


def _summary(dataset: Dataset) -> DatasetSummary:
    return DatasetSummary(name=dataset.name, description=dataset.description,
                          grain=dataset.grain, time_column=dataset.time_column,
                          metrics=[MetricInfo(name=m.name, description=m.description)
                                   for m in dataset.metrics])


def _fit_single_row(row: list[Any], max_chars: int) -> bool:
    """Halve the longest text cell until one row fits the result cap. True if anything was cut."""
    cut = False
    while len(json.dumps([row], default=str)) > max_chars:
        texts = [i for i, v in enumerate(row) if isinstance(v, str) and len(v) > 20]
        if not texts:
            break
        longest = max(texts, key=lambda i: len(row[i]))
        row[longest] = row[longest][: len(row[longest]) // 2] + "…[cut]"
        cut = True
    return cut


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)
