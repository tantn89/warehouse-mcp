"""What the SQL guard refuses or allows, by node type, function and table clause.

sql_guard.py applies these rules. Kept apart so the lists can grow per dialect without crowding
the rewrite logic; every addition needs a case in tests/test_red_team.py.
"""

from __future__ import annotations

from sqlglot import exp

# Statement and clause types that write, change session state or touch the filesystem.
FORBIDDEN_NODES: tuple[type[exp.Expr], ...] = tuple(
    getattr(exp, name) for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
        "Command", "Copy", "Pragma", "Set", "Use", "Attach", "Detach", "Export", "Into",
        "Lock", "Grant", "Revoke", "Transaction", "Commit", "Rollback", "Analyze", "Describe",
    ) if hasattr(exp, name)
)

# Functions the parser knows that read files, generate unbounded rows or reveal the session.
# Snowflake adds a row generator and a family of CURRENT_* functions that name the account,
# role, warehouse, session or client. Date and time CURRENT_* functions stay allowed.
DENIED_FUNCTIONS = {
    "ReadCSV", "ReadParquet", "ToFile", "GenerateSeries", "ExplodingGenerateSeries", "Explode",
    "_ExplodeOuter", "Unnest", "Generator", "CurrentUser", "SessionUser", "CurrentSchema",
    "CurrentSchemas", "CurrentCatalog", "CurrentDatabase", "CurrentVersion", "CurrentRole",
    "CurrentRoleType", "CurrentAvailableRoles", "CurrentSecondaryRoles", "CurrentAccount",
    "CurrentAccountName", "CurrentOrganizationName", "CurrentOrganizationUser", "CurrentRegion",
    "CurrentWarehouse", "CurrentSession", "CurrentStatement", "CurrentTransaction",
    "CurrentClient", "CurrentIpAddress", "CurrentUserId",
}

# A dataset reference may carry its name, qualifiers (refused separately) and a plain alias,
# nothing else. The policy rewrite replaces the table node with the role's view of it, so any
# other clause on the node (SAMPLE, PIVOT, a parenthesized join, AT/BEFORE time travel,
# PARTITION, hints, ...) would be dropped silently and the agent would get a different
# answer with no error. Closed world: a clause sqlglot learns to parse later is refused too.
# Time travel would not be safe to keep either: with the row filter still applied, it reads
# rows that were deleted or corrected, such as erased personal data still inside the
# retention window.
ALLOWED_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})
_TABLE_CLAUSES = {
    "sample": "SAMPLE / TABLESAMPLE", "pivots": "PIVOT / UNPIVOT",
    "joins": "A parenthesized join", "laterals": "LATERAL VIEW", "partition": "PARTITION",
    "hints": "A table hint", "only": "ONLY",
}
_TIME_TRAVEL = ("when", "changes", "version", "system_time")


def refused_table_clause(table: exp.Table) -> str | None:
    """Why a table reference cannot be rewritten into its policy view, or None if it can."""
    for arg, value in table.args.items():
        if not value or arg in ALLOWED_TABLE_ARGS:
            continue
        if arg in _TIME_TRAVEL:
            return ("Time travel (AT, BEFORE, CHANGES, FOR SYSTEM_TIME) is not allowed: "
                    "queries read the current data of each dataset.")
        clause = _TABLE_CLAUSES.get(arg, f"The {arg.upper()} clause")
        return (f"{clause} on a dataset is not allowed. Apply it to a subquery, e.g. "
                f"FROM (SELECT ... FROM {table.name}) AS t, or join the datasets directly.")
    alias = table.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return (f"Column aliases on a dataset ({alias.name}(...)) are not allowed; alias the "
                "columns in the select list.")
    return None

# Functions the parser does not model (so it sees an "Anonymous" call) are denied unless
# listed here or in the config: dialect-specific functions are where file reads
# (pg_read_file, read_text), sleeps and settings changes hide.
DEFAULT_ALLOWED_FUNCTIONS = frozenset({
    "dayname", "monthname", "make_date", "make_timestamp", "epoch_ms", "date_part",
    "isodow", "isoyear", "percentile_cont", "percentile_disc", "bool_or", "bool_and",
})
