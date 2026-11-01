"""Compile a metrics request (metrics, dimensions, filters, time range) into SQL.

This is the path agents should prefer. They name business concepts, and the server writes the
SQL as a syntax tree, so filter values are literals and can never become SQL. The result then
goes through the same guard as hand-written SQL, so policies apply identically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from warehouse_mcp.config import Dataset
from warehouse_mcp.sql_guard import QueryRejected

TimeGrain = Literal["minute", "hour", "day", "week", "month"]
Scalar = str | int | float | bool


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(description="A column of the dataset.")
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in", "is null", "is not null"]
    value: Scalar | list[Scalar] | None = Field(
        default=None, description="A value, or a list for in / not in; omit for is null.")


def compile_metrics_query(dataset: Dataset, metrics: list[str], dimensions: list[str],
                          filters: list[Filter], time_grain: TimeGrain | None,
                          since: datetime | None, until: datetime | None,
                          order_by: str | None, descending: bool, limit: int,
                          dialect: str) -> str:
    known_metrics = {m.name: m for m in dataset.metrics}
    unknown = [m for m in metrics if m not in known_metrics]
    if unknown or not metrics:
        raise QueryRejected(f"Unknown metrics {unknown} for {dataset.name}. "
                            f"Available: {', '.join(known_metrics) or 'none'}.")
    _require_columns(dataset, dimensions + [f.column for f in filters])

    projections: list[exp.Expr] = []
    group_by: list[exp.Expr] = []
    if time_grain:
        if not dataset.time_column:
            raise QueryRejected(f"{dataset.name} has no time column; drop time_grain.")
        period = sqlglot.parse_one(f"DATE_TRUNC('{time_grain}', {dataset.time_column})",
                                   read="duckdb")
        projections.append(period.as_("period"))
        group_by.append(exp.column("period"))
    for dim in dimensions:
        projections.append(exp.column(dim))
        group_by.append(exp.column(dim))
    for name in metrics:
        projections.append(sqlglot.parse_one(known_metrics[name].sql, read="duckdb").as_(name))

    query = exp.select(*projections).from_(dataset.name)
    for condition in _conditions(dataset, filters, since, until):
        query = query.where(condition)
    if group_by:
        query = query.group_by(*group_by)
    if order_by:
        if order_by not in {*metrics, *dimensions, *(["period"] if time_grain else [])}:
            raise QueryRejected("order_by must be one of the requested metrics or dimensions"
                                + (", or period." if time_grain else "."))
        query = query.order_by(exp.Ordered(this=exp.column(order_by), desc=descending))
    return query.limit(limit).sql(dialect=dialect)


def _require_columns(dataset: Dataset, names: list[str]) -> None:
    columns = {c.name for c in dataset.columns}
    unknown = sorted(set(names) - columns)
    if unknown:
        raise QueryRejected(f"Unknown columns {unknown} for {dataset.name}. "
                            f"Columns: {', '.join(sorted(columns))}.")


def _conditions(dataset: Dataset, filters: list[Filter], since: datetime | None,
                until: datetime | None) -> list[exp.Expr]:
    conditions = [_condition(f) for f in filters]
    if since or until:
        if not dataset.time_column:
            raise QueryRejected(f"{dataset.name} has no time column; drop since/until.")
        column = exp.column(dataset.time_column)
        if since:
            conditions.append(column >= _timestamp(since))
        if until:
            conditions.append(column < _timestamp(until))
    return conditions


def _condition(f: Filter) -> exp.Expr:
    column = exp.column(f.column)
    if f.op in ("is null", "is not null"):
        is_null = column.is_(exp.null())
        return is_null if f.op == "is null" else exp.not_(is_null)
    if f.op in ("in", "not in"):
        if not isinstance(f.value, list) or not f.value:
            raise QueryRejected(f"Filter {f.column} {f.op} needs a non-empty list value.")
        member = column.isin(*[exp.convert(v) for v in f.value])
        return member if f.op == "in" else exp.not_(member)
    if f.value is None or isinstance(f.value, list):
        raise QueryRejected(f"Filter {f.column} {f.op} needs a single value.")
    value = exp.convert(f.value)
    return {"=": column.eq, "!=": column.neq, ">": column.__gt__, ">=": column.__ge__,
            "<": column.__lt__, "<=": column.__le__}[f.op](value)


def _timestamp(value: datetime) -> exp.Expr:
    """A UTC timestamp literal: warehouse time columns are UTC; aware inputs are converted."""
    if value.tzinfo:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return exp.cast(exp.Literal.string(value.isoformat(sep=" ")),
                    exp.DataType.build("timestamp"))
