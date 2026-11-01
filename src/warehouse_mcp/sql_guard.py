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
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.scope import traverse_scope

from warehouse_mcp.config import Dataset, Role, Settings

# Statement and clause types that write, change session state or touch the filesystem.
_FORBIDDEN_NODES: tuple[type[exp.Expr], ...] = tuple(
    getattr(exp, name) for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
        "Command", "Copy", "Pragma", "Set", "Use", "Attach", "Detach", "Export", "Into",
        "Lock", "Grant", "Revoke", "Transaction", "Commit", "Rollback", "Analyze", "Describe",
    ) if hasattr(exp, name)
)

# Functions the parser knows that read files, generate unbounded rows or reveal the session.
_DENIED_FUNCTIONS = {
    "ReadCSV", "ReadParquet", "ToFile", "GenerateSeries", "ExplodingGenerateSeries", "Explode",
    "_ExplodeOuter", "Unnest", "CurrentUser", "SessionUser", "CurrentSchema", "CurrentCatalog",
    "CurrentDatabase", "CurrentVersion",
}

# Functions the parser does not model (so it sees an "Anonymous" call) are denied unless
# listed here or in the config: dialect-specific functions are where file reads
# (pg_read_file, read_text), sleeps and settings changes hide.
DEFAULT_ALLOWED_FUNCTIONS = frozenset({
    "dayname", "monthname", "make_date", "make_timestamp", "epoch_ms", "date_part",
    "isodow", "isoyear", "percentile_cont", "percentile_disc", "bool_or", "bool_and",
})


class QueryRejected(Exception):
    """The query was refused before reaching the warehouse. The message is safe to show."""


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
    references = _dataset_references(root, settings, role)
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
        if isinstance(node, _FORBIDDEN_NODES):
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
        elif type(func).__name__ in _DENIED_FUNCTIONS:
            raise QueryRejected(f"{func.sql_name()} is not allowed: queries may only read the "
                                "declared datasets.")


def _cte_references(root: exp.Expr) -> set[int]:
    """ids of the Table nodes that refer to a CTE.

    A table refers to a CTE when its *name* is a CTE visible in its own scope. Two shortcuts
    are wrong and were both exploitable:
    - a global name match: in `WITH t AS (SELECT * FROM t)` the inner `t` is the real table t;
    - the alias: in `WITH x AS (...) SELECT * FROM secret_table x` the real table is aliased
      to the CTE's name.
    Aliasing a real table to a visible CTE name is refused outright: it is never needed.
    """
    if any(w.args.get("recursive") for w in root.find_all(exp.With)):
        raise QueryRejected("WITH RECURSIVE is not allowed.")
    try:
        scopes = traverse_scope(root)
    except OptimizeError as exc:
        raise QueryRejected(f"Could not resolve the query's tables: {exc}") from exc
    cte_refs: set[int] = set()
    for scope in scopes:
        visible = {name.lower() for name in scope.cte_sources}
        for table in scope.tables:
            if table.args.get("db") or table.args.get("catalog"):
                continue  # qualified: never a CTE; rejected later
            if table.name.lower() in visible:
                cte_refs.add(id(table))
            elif table.alias and table.alias.lower() in visible:
                raise QueryRejected(f"Alias {table.alias!r} hides the CTE of the same name; "
                                    "use a different alias.")
    return cte_refs


def _dataset_references(root: exp.Expr, settings: Settings,
                        role: Role) -> list[tuple[exp.Table, Dataset]]:
    available = ", ".join(d.name for d in settings.datasets if role.can_read(d.name))
    cte_refs = _cte_references(root)
    references = []
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise QueryRejected("Table functions are not allowed in FROM; query the datasets: "
                                f"{available}.")
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
