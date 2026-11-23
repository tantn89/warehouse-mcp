"""The Snowflake connection model: names validated, no secret field, the key left to the env."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from test_postgres_parity import _environment
from warehouse_mcp.config import SnowflakeConnection, load_settings

SNOWFLAKE_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "snowflake.yaml"
BASE = {"kind": "snowflake", "account": "acme-demo", "user": "MCP_READER_SVC",
        "role": "MCP_READER", "warehouse": "MCP_WH"}


def test_example_config_loads_with_the_key_path_left_to_the_environment() -> None:
    with _environment(WAREHOUSE_MCP_DEMO_DB="unused", WAREHOUSE_MCP_SNOWFLAKE_ACCOUNT="acme-demo",
                      WAREHOUSE_MCP_SCHEMA="WAREHOUSE_MCP.WIKIPEDIA"):
        settings = load_settings(SNOWFLAKE_CONFIG)
    connection = settings.connection
    assert isinstance(connection, SnowflakeConnection)
    assert (connection.account, connection.role, connection.warehouse) == (
        "acme-demo", "MCP_READER", "MCP_WH")
    assert connection.private_key_file_env == "WAREHOUSE_MCP_SNOWFLAKE_KEY_FILE"
    edits = settings.dataset("edits")
    assert edits is not None and edits.table == "WAREHOUSE_MCP.WIKIPEDIA.edits"


def test_example_config_needs_the_account_from_the_environment(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAREHOUSE_MCP_SNOWFLAKE_ACCOUNT", raising=False)
    with (_environment(WAREHOUSE_MCP_DEMO_DB="unused"),
          pytest.raises(ValueError, match="WAREHOUSE_MCP_SNOWFLAKE_ACCOUNT")):
        load_settings(SNOWFLAKE_CONFIG)


def test_connection_defaults() -> None:
    connection = SnowflakeConnection.model_validate(BASE)
    assert connection.statement_timeout_s == 60
    assert connection.private_key_file_env == "WAREHOUSE_MCP_SNOWFLAKE_KEY_FILE"


@pytest.mark.parametrize("override", [
    {"private_key": "inline keys are not a field"},  # secrets never live in the config
    {"password": "x"},
    {"role": "MCP_READER; DROP"},
    {"warehouse": '"Quoted"'},
    {"database": "WAREHOUSE_MCP"},  # no default database: tables are fully qualified
    {"schema": "WIKIPEDIA"},
    {"account": "acme demo"},
    {"statement_timeout_s": 0},
    {"statement_timeout_s": 7200},
    {"user": ""},
])
def test_connection_rejects_secrets_and_malformed_names(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SnowflakeConnection.model_validate({**BASE, **override})
