"""BigQuery: dry-run first, then run with a hard cap on bytes billed.

Cost is the risk specific to a pay-per-scan warehouse. An agent that writes one careless
`SELECT *` over a large table can spend real money. Each query is priced with a free dry run.
A query above the cap is refused with a message that tells the agent how to narrow it. The
real run carries `maximum_bytes_billed`, so BigQuery itself enforces the cap as well.
Credentials come from Application Default Credentials; nothing is stored in the config.
"""

from __future__ import annotations

from warehouse_mcp.backends.base import BackendError, QueryResult

try:
    from google.api_core import exceptions as google_exceptions
    from google.cloud import bigquery
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError("BigQuery support needs: pip install 'warehouse-mcp[bigquery]'") from exc


class BigQueryBackend:
    dialect = "bigquery"

    def __init__(self, project: str, location: str, maximum_bytes_billed: int) -> None:
        self._client = bigquery.Client(project=project, location=location)
        self._max_bytes = maximum_bytes_billed

    def execute(self, sql: str, max_rows: int, timeout_s: float) -> QueryResult:
        try:
            dry = self._client.query(sql, job_config=bigquery.QueryJobConfig(
                dry_run=True, use_query_cache=False))
            estimate = int(dry.total_bytes_processed or 0)
            if estimate > self._max_bytes:
                raise BackendError(
                    f"Query would scan {estimate / 1e9:.2f} GB, above the "
                    f"{self._max_bytes / 1e9:.2f} GB cap. Select fewer columns or filter on "
                    "the time column.")
            job = self._client.query(sql, job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=self._max_bytes, labels={"app": "warehouse-mcp"}),
                timeout=timeout_s)
            result = job.result(timeout=timeout_s, max_results=max_rows)
            rows = [tuple(row.values()) for row in result]
            columns = [field.name for field in result.schema]
        except BackendError:
            raise
        except google_exceptions.GoogleAPICallError as exc:
            raise BackendError(_message(exc)) from exc
        except TimeoutError as exc:
            raise BackendError(f"Query exceeded the {timeout_s:.0f}s limit.") from exc
        return QueryResult(columns=columns, rows=rows[:max_rows],
                           bytes_processed=int(job.total_bytes_processed or 0))

    def table_columns(self, table: str) -> dict[str, str]:
        return {f.name: f.field_type for f in self._client.get_table(table).schema}

    def close(self) -> None:
        self._client.close()  # type: ignore[no-untyped-call]


def _message(exc: google_exceptions.GoogleAPICallError) -> str:
    errors = getattr(exc, "errors", None) or []
    first = errors[0].get("message") if errors and isinstance(errors[0], dict) else None
    return str(first or exc.message or "Query failed.")
