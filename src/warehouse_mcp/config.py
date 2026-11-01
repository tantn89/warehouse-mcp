"""Server configuration: connection, semantic model and access roles, loaded from YAML.

Everything an agent can see is declared here. A dataset exposes only the columns listed for
it, a role sees only the datasets it is granted, and every value that identifies a person is
tagged so the policy layer can pseudonymize it before it leaves the warehouse.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DuckDBConnection(_Strict):
    kind: Literal["duckdb"]
    path: str = Field(description="DuckDB database file, opened read-only.")


class PostgresConnection(_Strict):
    kind: Literal["postgres"]
    dsn_env: str = Field(default="WAREHOUSE_MCP_POSTGRES_DSN",
                         description="Environment variable holding the connection string.")


class BigQueryConnection(_Strict):
    kind: Literal["bigquery"]
    project: str
    location: str = "US"
    maximum_bytes_billed: int = Field(default=1_000_000_000, ge=10_485_760,
                                      description="Hard cap per query; BigQuery refuses above it.")


Connection = Annotated[DuckDBConnection | PostgresConnection | BigQueryConnection,
                       Field(discriminator="kind")]


class Column(_Strict):
    name: Name
    description: str = ""
    pii: bool = Field(default=False, description="Identifies a person: pseudonymized unless "
                                                 "the role is cleared.")
    free_text: bool = Field(default=False, description="Human-written text: scanned for "
                                                       "emails and phone numbers.")


class Metric(_Strict):
    name: Name
    sql: str = Field(description="Aggregate expression over the dataset's columns.")
    description: str = ""


class Dataset(_Strict):
    name: Name
    table: str = Field(description="Physical table in the backend, e.g. main.edits.")
    description: str
    grain: str = Field(description="What one row represents.")
    time_column: Name | None = None
    columns: list[Column] = Field(min_length=1)
    metrics: list[Metric] = []

    @model_validator(mode="after")
    def _names_are_unique(self) -> Dataset:
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"dataset {self.name}: duplicate column names")
        if self.time_column and self.time_column not in names:
            raise ValueError(f"dataset {self.name}: time_column {self.time_column} is not a column")
        return self

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


class Role(_Strict):
    name: Name
    datasets: list[Name] | Literal["*"] = "*"
    row_filters: dict[Name, str] = Field(
        default={}, description="Dataset -> SQL predicate every query on it is restricted to.")
    pii_clearance: bool = False
    api_key_env: str | None = Field(
        default=None, description="HTTP transport: env var holding this role's bearer key.")

    def can_read(self, dataset: str) -> bool:
        return self.datasets == "*" or dataset in self.datasets


class Limits(_Strict):
    max_rows: int = Field(default=500, ge=1, le=10_000)
    timeout_s: float = Field(default=20.0, gt=0, le=300)
    max_result_chars: int = Field(default=40_000, ge=1_000)


class Settings(_Strict):
    name: str = "warehouse"
    description: str = ""
    connection: Connection
    datasets: list[Dataset] = Field(min_length=1)
    roles: list[Role] = Field(min_length=1)
    default_role: Name
    limits: Limits = Limits()
    audit_log: str | None = Field(default=None, description="JSONL file; stderr if unset.")
    allowed_functions: list[str] = Field(
        default=[], description="Extra backend functions (unknown to the SQL parser) to allow.")

    @model_validator(mode="after")
    def _references_resolve(self) -> Settings:
        datasets = {d.name for d in self.datasets}
        if len(datasets) != len(self.datasets):
            raise ValueError("duplicate dataset names")
        roles = {r.name: r for r in self.roles}
        if self.default_role not in roles:
            raise ValueError(f"default_role {self.default_role} is not a role")
        for role in self.roles:
            granted = datasets if role.datasets == "*" else set(role.datasets)
            unknown = (granted | set(role.row_filters)) - datasets
            if unknown:
                raise ValueError(f"role {role.name} references unknown datasets {sorted(unknown)}")
        return self

    def dataset(self, name: str) -> Dataset | None:
        return next((d for d in self.datasets if d.name == name), None)

    def role(self, name: str) -> Role:
        role = next((r for r in self.roles if r.name == name), None)
        if role is None:
            raise KeyError(f"unknown role {name}")
        return role


_ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(text: str) -> str:
    """Expand ${VAR} and ${VAR:-default} so configs carry no secrets or machine paths."""
    def replace(match: re.Match[str]) -> str:
        value = os.environ.get(match.group(1), match.group(2))
        if value is None:
            raise ValueError(f"environment variable {match.group(1)} is not set")
        return value

    return _ENV_REF.sub(replace, text)


def load_settings(path: str | Path) -> Settings:
    raw = yaml.safe_load(_expand_env(Path(path).read_text(encoding="utf-8")))
    return Settings.model_validate(raw)
