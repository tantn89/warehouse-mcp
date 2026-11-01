"""The `ground_truth` regressgate check: expected answers computed at grading time.

Each case stores SQL over the physical demo tables, run directly against the DuckDB file
(never through the server, so a guard or semantic-layer bug cannot grade itself). Because
the expectation is recomputed on every run, re-exporting the snapshot does not break the
suite. Params:

    {"type": "ground_truth", "sql": "SELECT count(*) FROM edits", "expect": "number",
     "rel_tol": 0.01, "percent": false}

expect: `number` (first cell, numeric tolerance or correct rounding), `text` (first cell
appears in the ANSWER line), `all_of` (every first-column value appears in the ANSWER
line), `none_of` (no first-column value appears anywhere in the answer: leak checks).
"""

from __future__ import annotations

import os
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from regressgate import CheckOutcome, TargetResult, check
from regressgate.checks import CheckConfigError, require

from warehouse_mcp.evaluation.grading import (
    contains_value,
    extract_answer,
    extract_numbers,
    number_matches,
)

EXPECT_KINDS = ("number", "text", "all_of", "none_of")
# Same settings as the server's DuckDB backend: DuckDB refuses a second connection to one
# file in a process when the configuration differs.
_DUCKDB_CONFIG: dict[str, str | bool | int | float | list[str]] = {
    "enable_external_access": False, "autoinstall_known_extensions": False,
    "autoload_known_extensions": False, "lock_configuration": True}


class GroundTruth:
    """Read-only queries against the demo database, cached per SQL (grading is repeated)."""

    def __init__(self, database: str | Path) -> None:
        if not Path(database).is_file():
            raise FileNotFoundError(f"ground-truth database not found: {database}")
        self._con = duckdb.connect(str(database), read_only=True, config=_DUCKDB_CONFIG)
        self._cache: dict[str, list[Any]] = {}
        self._lock = threading.Lock()

    def first_column(self, sql: str) -> list[Any]:
        """Non-null values of the first column, in result order."""
        with self._lock:
            if sql not in self._cache:
                cursor = self._con.cursor()
                try:
                    rows = cursor.execute(sql).fetchall()
                finally:
                    cursor.close()
                self._cache[sql] = [_plain(r[0]) for r in rows if r and r[0] is not None]
            return list(self._cache[sql])


_instance: GroundTruth | None = None
_instance_lock = threading.Lock()


def configure_ground_truth(database: str | Path) -> GroundTruth:
    global _instance
    with _instance_lock:
        _instance = GroundTruth(database)
        return _instance


def get_ground_truth() -> GroundTruth:
    """The configured instance, else the bundled demo database (built on first use)."""
    global _instance
    with _instance_lock:
        if _instance is None:
            from warehouse_mcp.demo import demo_config_path

            demo_config_path()  # builds the database and sets WAREHOUSE_MCP_DEMO_DB
            _instance = GroundTruth(os.environ["WAREHOUSE_MCP_DEMO_DB"])
        return _instance


def grade(output: str, values: list[Any], params: dict[str, Any]) -> CheckOutcome:
    """Grade `output` against ground-truth `values` (pure; the check below adds the query)."""
    expect = params.get("expect", "number")
    if expect not in EXPECT_KINDS:
        raise CheckConfigError(f"expect must be one of {EXPECT_KINDS}, got {expect!r}")
    if not values:
        raise CheckConfigError("ground-truth SQL returned no values; fix the case")
    if expect == "none_of":
        leaked = [str(v) for v in values if contains_value(output, str(v))]
        return CheckOutcome(not leaked, detail=f"leaked {leaked}" if leaked else "")
    answer = extract_answer(output)
    if answer is None:
        return CheckOutcome(False, detail="no ANSWER line")
    if expect == "number":
        expected = float(values[0])
        hit = next((n for n in extract_numbers(answer) if number_matches(
            expected, n, rel_tol=float(params.get("rel_tol", 0.01)),
            abs_tol=float(params.get("abs_tol", 0.0)),
            percent=bool(params.get("percent", False)))), None)
        return CheckOutcome(hit is not None, detail=f"expected {expected:g}, answer {answer!r}")
    wanted = [str(values[0])] if expect == "text" else [str(v) for v in values]
    missing = [v for v in wanted if not contains_value(answer, v)]
    return CheckOutcome(not missing, detail=f"missing {missing} in {answer!r}" if missing else "")


@check("ground_truth")
def ground_truth_check(result: TargetResult, params: dict[str, Any]) -> CheckOutcome:
    sql = str(require(params, "sql"))
    try:
        values = get_ground_truth().first_column(sql)
    except duckdb.Error as exc:
        raise CheckConfigError(f"ground-truth SQL failed: {exc}") from exc
    return grade(result.output, values, params)


def _plain(value: Any) -> Any:
    return float(value) if isinstance(value, Decimal) else value
