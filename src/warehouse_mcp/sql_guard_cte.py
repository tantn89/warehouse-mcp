"""Which table references in a query are CTEs, decided the way the engine decides.

Used by sql_guard.py: a reference that is a CTE is left alone, every other one is
rewritten into its policy view. Getting this wrong in either direction is a bypass.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.scope import traverse_scope

from warehouse_mcp.config import Settings
from warehouse_mcp.sql_guard_rules import QueryRejected


def cte_references(root: exp.Expr, settings: Settings, dialect: str) -> set[int]:
    """ids of the Table nodes that refer to a CTE.

    A table refers to a CTE when its *name* is a CTE visible in its own scope. Three shortcuts
    are wrong and were all exploitable:
    - a global name match: in `WITH t AS (SELECT * FROM t)` the inner `t` is the real table t;
    - the alias: in `WITH x AS (...) SELECT * FROM secret_table x` the real table is aliased
      to the CTE's name;
    - a case-insensitive match: in Postgres `WITH "EDITS" AS (...) SELECT * FROM edits`, and
      in Snowflake `WITH "edits" AS (...)`, the engine does not resolve `edits` to the CTE but
      to the real table, which then skipped the policy rewrite.
    Names are compared the way the engine resolves them (sqlglot's identifier normalization
    for the dialect). Where that and a case-insensitive comparison disagree, the query is
    refused instead of guessed at, and so is aliasing a real table to a visible CTE name.
    """
    if any(w.args.get("recursive") for w in root.find_all(exp.With)):
        raise QueryRejected("WITH RECURSIVE is not allowed.")
    normalize = Dialect.get_or_raise(dialect).normalize_identifier

    def key(identifier: exp.Identifier) -> str:
        return normalize(identifier.copy()).name

    # A CTE named like a dataset must resolve exactly as the dataset name written plainly
    # would; `"EDITS"` (Postgres) or `"edits"` (Snowflake) is never needed.
    datasets = {d.name: key(exp.to_identifier(d.name)) for d in settings.datasets}
    for cte in root.find_all(exp.CTE):
        name = cte.args["alias"].this
        plain = datasets.get(name.name.lower())
        if plain is not None and key(name) != plain:
            raise QueryRejected(f"CTE name {cte.alias!r} differs from the dataset "
                                f"{name.name.lower()} only by case; use another name.")
    try:
        scopes = traverse_scope(root)
    except OptimizeError as exc:
        raise QueryRejected(f"Could not resolve the query's tables: {exc}") from exc
    cte_refs: set[int] = set()
    for scope in scopes:
        visible = {}
        for name, source in scope.cte_sources.items():
            node = source.expression.parent  # the CTE node; its alias keeps the quoting
            if not isinstance(node, exp.CTE):
                raise QueryRejected(f"Could not resolve the CTE {name!r}.")
            visible[name.lower()] = key(node.args["alias"].this)
        for table in scope.tables:
            if (table.args.get("db") or table.args.get("catalog")
                    or not isinstance(table.this, exp.Identifier)):
                continue  # qualified names and table functions: never a CTE; rejected later
            cte_key = visible.get(table.name.lower())
            if cte_key is not None:
                if key(table.this) != cte_key:
                    raise QueryRejected(f"Table {table.name!r} and the CTE of that name differ "
                                        "only by case or quoting; use one spelling.")
                cte_refs.add(id(table))
            elif table.alias and table.alias.lower() in visible:
                raise QueryRejected(f"Alias {table.alias!r} hides the CTE of the same name; "
                                    "use a different alias.")
    return cte_refs
