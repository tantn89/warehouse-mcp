"""Turn agent-written SQL into a query that can only read what the caller's role may see.

The guard parses the SQL in the backend's dialect and rejects anything that is not a single
read-only query over declared datasets. It then rewrites every dataset reference into a
policy subquery, which:
- projects only the declared columns;
- pseudonymizes columns that identify a person, unless the role is cleared;
- applies the role's mandatory row filter.

Masking happens inside the warehouse, before any value leaves it. `count(DISTINCT editor)` and
GROUP BY on a pseudonym stay exact, while names, and probes like `WHERE editor = 'Alice'`,
reveal nothing.

This is one layer. Each backend adds its own: read-only connections, disabled file and network
access, timeouts and byte caps. A gap in the parser alone is therefore not a breach.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from warehouse_mcp.config import Dataset, Role, Settings
from warehouse_mcp.sql_guard_cte import cte_references
from warehouse_mcp.sql_guard_rules import (
    DEFAULT_ALLOWED_FUNCTIONS,
    DENIED_FUNCTIONS,
    FORBIDDEN_NODES,
    QueryRejected,
    refused_table_clause,
)

# QueryRejected lives with the rules so sql_guard_cte.py can raise it; callers import it here.
__all__ = ["DEFAULT_ALLOWED_FUNCTIONS", "GuardedQuery", "QueryRejected", "guard_query",
           "policy_source", "pseudonym"]


@dataclass(frozen=True)
class GuardedQuery:
    physical_sql: str  # what runs: policy subqueries, salt and row filters inlined
    logical_sql: str  # what the agent asked, normalized and limited: safe to return and log
    datasets: tuple[str, ...]
    fetch_limit: int  # rows requested from the backend: max_rows + 1 detects truncation


def guard_query(sql: str, settings: Settings, role: Role, dialect: str,
                pii_salt: str) -> GuardedQuery:
    root = _parse_single_query(sql, dialect)
    _reject_writes_and_side_effects(root)
    _check_functions(root, settings)
    references = _dataset_references(root, settings, role, dialect)
    fetch_limit = _apply_limit(root, settings.limits.max_rows)
    logical_sql = root.sql(dialect=dialect)

    for table, dataset in references:
        table.replace(policy_source(dataset, role, dialect, pii_salt,
                                    alias=table.alias_or_name))
    return GuardedQuery(physical_sql=root.sql(dialect=dialect), logical_sql=logical_sql,
                        datasets=tuple(sorted({d.name for _, d in references})),
                        fetch_limit=fetch_limit)


def _parse_single_query(sql: str, dialect: str) -> exp.Query:
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as exc:
        raise QueryRejected(f"Could not parse the SQL as {dialect}: {exc.errors[0]['description']}"
                            if exc.errors else f"Could not parse the SQL as {dialect}.") from exc
    if len(statements) != 1:
        raise QueryRejected("Send exactly one statement; multiple statements are not allowed.")
    root = statements[0]
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise QueryRejected("Only read queries (SELECT, WITH ... SELECT, UNION) are allowed.")
    return root


def _reject_writes_and_side_effects(root: exp.Expr) -> None:
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise QueryRejected(f"{type(node).__name__} is not allowed: this server is read-only.")
        if isinstance(node, exp.Select) and (node.args.get("into") or node.args.get("locks")):
            raise QueryRejected("SELECT INTO and row locks are not allowed: this server is "
                                "read-only.")


def _check_functions(root: exp.Expr, settings: Settings) -> None:
    allowed = DEFAULT_ALLOWED_FUNCTIONS | {f.lower() for f in settings.allowed_functions}
    for func in root.find_all(exp.Func):
        if isinstance(func, exp.Anonymous):
            if func.name.lower() not in allowed:
                raise QueryRejected(f"Function {func.name}() is not allowed. Use standard SQL "
                                    "functions (aggregates, date/time, string, math, CASE).")
        elif type(func).__name__ in DENIED_FUNCTIONS:
            raise QueryRejected(f"{func.sql_name()} is not allowed: queries may only read the "
                                "declared datasets.")


def _dataset_references(root: exp.Expr, settings: Settings, role: Role,
                        dialect: str) -> list[tuple[exp.Table, Dataset]]:
    available = ", ".join(d.name for d in settings.datasets if role.can_read(d.name))
    cte_refs = cte_references(root, settings, dialect)
    # Snowflake TABLE(...) (RESULT_SCAN, GENERATOR, FLATTEN) and LATERAL over a function.
    if root.find(exp.TableFromRows) or any(not isinstance(lateral.this, exp.Subquery)
                                           for lateral in root.find_all(exp.Lateral)):
        raise QueryRejected("Table functions are not allowed in FROM; query the datasets: "
                            f"{available}.")
    references = []
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):  # stages (@stage), IDENTIFIER(), calls
            raise QueryRejected("Table functions are not allowed in FROM; query the datasets: "
                                f"{available}.")
        refusal = refused_table_clause(table)
        if refusal:
            raise QueryRejected(refusal)
        if table.args.get("db") or table.args.get("catalog"):
            raise QueryRejected(f"Use dataset names without a schema prefix: {available}.")
        if id(table) in cte_refs:
            continue
        name = table.name.lower()
        dataset = settings.dataset(name)
        if dataset is None:
            raise QueryRejected(f"Unknown dataset {table.name!r}. Available: {available}.")
        if not role.can_read(dataset.name):
            raise QueryRejected(f"Role {role.name} may not read {dataset.name}. "
                                f"Available: {available}.")
        references.append((table, dataset))
    if not references:
        raise QueryRejected(f"The query reads no dataset. Available: {available}.")
    return references


def _apply_limit(root: exp.Query, max_rows: int) -> int:
    """Cap rows at max_rows + 1, so one extra row tells the caller the result was truncated."""
    cap = max_rows + 1
    limit = root.args.get("limit")
    if limit is not None:
        value = limit.expression
        if not (isinstance(value, exp.Literal) and value.is_int):
            raise QueryRejected("LIMIT must be a whole number.")
        cap = min(cap, int(value.name))
    root.limit(cap, copy=False)
    return cap


def pseudonym(column: str, salt: str) -> exp.Expr:
    """A stable, salted pseudonym: equal inputs stay equal (joins, counts), names never leave."""
    template = (f"CASE WHEN {column} IS NULL THEN NULL ELSE CONCAT('pii_', "
                f"SUBSTRING(MD5(CONCAT('{salt}', CAST({column} AS TEXT))), 1, 12)) END")
    return sqlglot.parse_one(template, read="duckdb")


def policy_source(dataset: Dataset, role: Role, dialect: str, pii_salt: str,
                  alias: str) -> exp.Subquery:
    """The only view of a dataset a role gets: declared columns, pseudonyms, its row filter."""
    projections = []
    for col in dataset.columns:
        value: exp.Expr = exp.column(col.name)
        if not role.pii_clearance:
            if col.pii:
                value = pseudonym(col.name, pii_salt)
            elif col.pii_when:
                value = exp.Case(ifs=[exp.If(this=sqlglot.condition(col.pii_when, dialect=dialect),
                                            true=pseudonym(col.name, pii_salt))],
                                 default=value)
        projections.append(value.as_(col.name))
    select = exp.select(*projections).from_(exp.to_table(dataset.table, dialect=dialect))
    row_filter = role.row_filters.get(dataset.name)
    if row_filter:
        select = select.where(sqlglot.condition(row_filter, dialect=dialect))
    return select.subquery(alias)
