"""What the server needs from a warehouse: run one bounded read query, describe a table."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class BackendError(Exception):
    """The warehouse refused or failed the query. The message is safe to show the agent."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    bytes_processed: int | None = None  # reported by warehouses that bill by bytes scanned


class Backend(Protocol):
    dialect: str  # sqlglot dialect name

    def execute(self, sql: str, max_rows: int, timeout_s: float) -> QueryResult:
        """Run a guarded query; return at most max_rows rows; give up after timeout_s."""
        ...

    def table_columns(self, table: str) -> dict[str, str]:
        """Column name -> type for a physical table, to validate the config at startup."""
        ...

    def close(self) -> None: ...
