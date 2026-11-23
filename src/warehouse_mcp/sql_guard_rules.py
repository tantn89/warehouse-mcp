"""What the SQL guard refuses or allows, by node type, function and table clause.

Data only: sql_guard.py applies these rules. Kept apart so the lists can grow per dialect
without crowding the rewrite logic; every addition needs a case in tests/test_red_team.py.
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

# Table clauses that read another version of a table: Snowflake AT(...), BEFORE(...) and
# CHANGES(...), and FOR SYSTEM_TIME AS OF. The policy rewrite replaces the table and would
# drop them silently, answering about current data. Kept on the physical table instead, the
# row filter would still apply, but time travel reads rows that were deleted or corrected,
# such as erased personal data still inside the retention window. Refused outright.
VERSION_CLAUSES = ("when", "changes", "version")

# Functions the parser does not model (so it sees an "Anonymous" call) are denied unless
# listed here or in the config: dialect-specific functions are where file reads
# (pg_read_file, read_text), sleeps and settings changes hide.
DEFAULT_ALLOWED_FUNCTIONS = frozenset({
    "dayname", "monthname", "make_date", "make_timestamp", "epoch_ms", "date_part",
    "isodow", "isoyear", "percentile_cont", "percentile_disc", "bool_or", "bool_and",
})
