"""MCP tools over WarehouseService. Each tool resolves the caller's role, then delegates.

stdio serves one local user with the configured default role. Streamable HTTP authenticates
every request with a bearer key, and the key decides the role, so two analysts with different
keys see different datasets, rows and pseudonyms.
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, Field

from warehouse_mcp import __version__
from warehouse_mcp.backends import BackendError
from warehouse_mcp.config import Role, Settings
from warehouse_mcp.semantic import Filter, TimeGrain
from warehouse_mcp.service import DatasetDetail, DatasetSummary, QueryOutput, WarehouseService
from warehouse_mcp.sql_guard import QueryRejected

INSTRUCTIONS = """Read-only, governed access to a data warehouse.
Start with warehouse_list_datasets, then warehouse_describe_dataset for columns and sample rows.
Prefer warehouse_query_metrics: it uses the curated metric definitions. Use warehouse_run_sql
only when metrics and dimensions cannot express the question.
Values starting with 'pii_' are pseudonyms for people: equal pseudonyms are the same person,
so counting and grouping work, but they cannot be reversed. Do not try to recover names.
All times are UTC. Results are capped; `truncated: true` means narrow the query."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)


class DatasetList(BaseModel):
    datasets: list[DatasetSummary]


class BearerKeyVerifier(TokenVerifier):
    """Static bearer keys, one per role, read from the environment variables the config names."""

    def __init__(self, settings: Settings) -> None:
        self._keys = {}
        for role in settings.roles:
            key = os.environ.get(role.api_key_env) if role.api_key_env else None
            if key:
                self._keys[role.name] = key
        if not self._keys:
            raise SystemExit("HTTP transport needs at least one role with api_key_env set and "
                             "that environment variable defined.")

    async def verify_token(self, token: str) -> AccessToken | None:
        for role, key in self._keys.items():
            if hmac.compare_digest(token.encode(), key.encode()):
                return AccessToken(token=token, client_id=role, scopes=["warehouse:read"])
        return None


def build_server(service: WarehouseService, *, http_url: str | None = None) -> MCPServer:
    settings = service.settings
    auth_kwargs: dict[str, object] = {}
    if http_url:
        auth_kwargs = {
            "token_verifier": BearerKeyVerifier(settings),
            "auth": AuthSettings(issuer_url=AnyHttpUrl(http_url),
                                 resource_server_url=AnyHttpUrl(f"{http_url}/mcp"),
                                 required_scopes=["warehouse:read"]),
        }
    mcp = MCPServer("warehouse_mcp", title=f"Warehouse: {settings.name}",
                    description=settings.description or None, instructions=INSTRUCTIONS,
                    version=__version__, **auth_kwargs)  # type: ignore[arg-type]

    def caller() -> Role:
        token = get_access_token()
        return settings.role(token.client_id if token else settings.default_role)

    def tool_error(exc: QueryRejected | BackendError) -> ToolError:
        return ToolError(str(exc))

    @mcp.tool(title="List datasets", annotations=READ_ONLY)
    def warehouse_list_datasets() -> DatasetList:
        """List the datasets you may query, with what one row means and their named metrics.

        Call this first. Then use warehouse_describe_dataset for columns and sample rows.
        """
        return DatasetList(datasets=service.list_datasets(caller()))

    @mcp.tool(title="Describe dataset", annotations=READ_ONLY)
    def warehouse_describe_dataset(
        dataset: Annotated[str, Field(min_length=1, max_length=63,
                                      description="Dataset name from warehouse_list_datasets.")],
    ) -> DatasetDetail:
        """Columns (types, descriptions, which are pseudonymized), metrics and 3 sample rows.

        row_policy: true means your role only sees a subset of rows.
        """
        try:
            return service.describe_dataset(caller(), dataset)
        except (QueryRejected, BackendError) as exc:
            raise tool_error(exc) from exc

    @mcp.tool(title="Query metrics", annotations=READ_ONLY)
    def warehouse_query_metrics(
        dataset: Annotated[str, Field(min_length=1, max_length=63)],
        metrics: Annotated[list[str], Field(min_length=1, max_length=10,
                                            description="Metric names from the dataset.")],
        dimensions: Annotated[list[str], Field(max_length=5,
                                               description="Columns to group by.")] = [],  # noqa: B006
        filters: Annotated[list[Filter], Field(max_length=10)] = [],  # noqa: B006
        time_grain: Annotated[TimeGrain | None, Field(
            description="Bucket the time column; adds a `period` column.")] = None,
        since: Annotated[datetime | None, Field(description="Inclusive UTC start, ISO 8601.")
                         ] = None,
        until: Annotated[datetime | None, Field(description="Exclusive UTC end, ISO 8601.")
                         ] = None,
        order_by: Annotated[str | None, Field(
            description="A requested metric, dimension or `period`.")] = None,
        descending: bool = True,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> QueryOutput:
        """Aggregate named metrics by dimensions and optional time buckets. Preferred path.

        The server writes the SQL, so filter values are always safe literals. Errors name the
        valid metrics or columns; fix the request and retry.
        """
        try:
            return service.query_metrics(caller(), dataset, metrics, dimensions, filters,
                                         time_grain, since, until, order_by, descending, limit)
        except (QueryRejected, BackendError) as exc:
            raise tool_error(exc) from exc

    @mcp.tool(title="Run SQL", annotations=READ_ONLY)
    def warehouse_run_sql(
        sql: Annotated[str, Field(min_length=1, max_length=20_000, description=(
            "One SELECT over dataset names (not physical tables), in the warehouse dialect."))],
    ) -> QueryOutput:
        """Run one read-only SELECT when warehouse_query_metrics cannot express the question.

        Only declared datasets and columns exist. Writes, file access, system catalogs and
        unknown functions are refused with the reason. Rows are capped (see `truncated`).
        """
        try:
            return service.run_sql(caller(), sql)
        except (QueryRejected, BackendError) as exc:
            raise tool_error(exc) from exc

    return mcp
