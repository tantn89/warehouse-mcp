"""The Snowflake backend against a scripted connector: connection settings, row cap,
column-name case, timeouts and scrubbed errors.

The fake replaces `snowflake.connector.connect` only; the backend's own code, and the
connector's real error classes, are what run. Whether Snowflake honours these settings is
checked live in tests/test_snowflake_parity.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

connector = pytest.importorskip("snowflake.connector")

from snowflake.connector import errors as snowflake_errors  # noqa: E402

from warehouse_mcp.backends import BackendError, open_backend  # noqa: E402
from warehouse_mcp.backends.snowflake_backend import SnowflakeBackend  # noqa: E402
from warehouse_mcp.config import SnowflakeConnection  # noqa: E402
from warehouse_mcp.service import scrub_error  # noqa: E402

KEY_ENV = "WAREHOUSE_MCP_TEST_SNOWFLAKE_KEY_FILE"
CONNECTION = SnowflakeConnection.model_validate({
    "kind": "snowflake", "account": "acme-demo", "user": "MCP_READER_SVC", "role": "MCP_READER",
    "warehouse": "MCP_WH",
    "private_key_file_env": KEY_ENV, "statement_timeout_s": 60})


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self._params: dict[str, Any] = {}
        self._rows: list[tuple[Any, ...]] = []
        self.description: list[tuple[str, int]] | None = None

    def set_statement_parameter(self, key: str, value: Any) -> None:
        self._params[key] = value

    def execute(self, sql: str) -> FakeCursor:
        self._conn.executed.append((sql, dict(self._params)))
        if self._conn.on_execute is not None:
            self._conn.on_execute()
        if self._conn.error is not None:
            raise self._conn.error
        self.description = [(name, 0) for name in self._conn.columns]
        self._rows = list(self._conn.rows)
        return self

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        self._conn.fetch_sizes.append(size)
        return self._rows[:size]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        self._conn.closed_cursors += 1


class FakeConnection:
    def __init__(self) -> None:
        self.columns: list[str] = []
        self.rows: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.fetch_sizes: list[int] = []
        self.closed_cursors = 0
        self.closed = False
        self.connect_calls: list[dict[str, Any]] = []
        self.cursor_error: Exception | None = None  # e.g. the connector's closed connection
        self.on_execute: Callable[[], None] | None = None

    def cursor(self) -> FakeCursor:
        if self.cursor_error is not None:
            raise self.cursor_error
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "reader.p8"
    path.write_text("placeholder: the fake connector never reads it", encoding="utf-8")
    monkeypatch.setenv(KEY_ENV, str(path))
    return path


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, key_file: Path) -> Iterator[FakeConnection]:
    conn = FakeConnection()

    def connect(**kwargs: Any) -> FakeConnection:
        conn.connect_calls.append(kwargs)
        return conn

    monkeypatch.setattr(connector, "connect", connect)
    yield conn


def test_connects_with_key_pair_auth_and_pinned_session(fake: FakeConnection,
                                                        key_file: Path) -> None:
    backend = open_backend(CONNECTION)
    assert isinstance(backend, SnowflakeBackend) and backend.dialect == "snowflake"
    kwargs = fake.connect_calls[0]
    assert kwargs["authenticator"] == "SNOWFLAKE_JWT"
    assert kwargs["private_key_file"] == str(key_file)
    assert not {"password", "private_key", "token"} & set(kwargs)
    assert kwargs["client_session_keep_alive"] is False
    assert (kwargs["account"], kwargs["user"], kwargs["role"], kwargs["warehouse"]) == (
        "acme-demo", "MCP_READER_SVC", "MCP_READER", "MCP_WH")
    # No current database or schema: an unrewritten reference resolves to nothing.
    assert not {"database", "schema"} & set(kwargs)
    assert kwargs["session_parameters"] == {
        "QUERY_TAG": "warehouse-mcp", "STATEMENT_TIMEOUT_IN_SECONDS": 60, "TIMEZONE": "UTC",
        "TIMESTAMP_TYPE_MAPPING": "TIMESTAMP_NTZ"}
    backend.close()
    assert fake.closed


def test_needs_the_key_file_from_the_environment(monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(BackendError, match=KEY_ENV):
        SnowflakeBackend(CONNECTION)
    monkeypatch.setenv(KEY_ENV, str(tmp_path / "missing.p8"))
    with pytest.raises(BackendError, match="does not exist") as excinfo:
        SnowflakeBackend(CONNECTION)
    assert str(tmp_path) not in str(excinfo.value)


def test_column_names_are_lower_cased_and_rows_capped(fake: FakeConnection) -> None:
    fake.columns = ["WIKI", "COUNT(*)", "MixedCase"]
    fake.rows = [("enwiki", n, True) for n in range(10)]
    result = SnowflakeBackend(CONNECTION).execute("SELECT ...", 3, 20)
    # Quoted mixed-case aliases are lower-cased too: agents see the config's names.
    assert result.columns == ["wiki", "count(*)", "mixedcase"]
    assert result.rows == [("enwiki", 0, True), ("enwiki", 1, True), ("enwiki", 2, True)]
    assert fake.fetch_sizes == [3] and result.bytes_processed is None
    assert fake.closed_cursors == 1


@pytest.mark.parametrize(("timeout_s", "seconds"), [(2.5, 3), (0.2, 1), (20, 20), (500, 60)])
def test_each_statement_carries_the_timeout_capped_by_the_session(
        fake: FakeConnection, timeout_s: float, seconds: int) -> None:
    SnowflakeBackend(CONNECTION).execute("SELECT 1", 1, timeout_s)
    assert fake.executed == [("SELECT 1", {"STATEMENT_TIMEOUT_IN_SECONDS": seconds})]


@pytest.mark.parametrize(("errno", "sqlstate"), [(630, "57014"), (604, "57014"), (630, None)])
def test_a_cancelled_statement_reports_the_limit(fake: FakeConnection, errno: int,
                                                 sqlstate: str | None) -> None:
    fake.error = snowflake_errors.ProgrammingError(
        msg="Statement reached its statement or warehouse timeout of 3 second(s) and was "
            "canceled.", errno=errno, sqlstate=sqlstate)
    with pytest.raises(BackendError, match=r"exceeded the 3s limit"):
        SnowflakeBackend(CONNECTION).execute("SELECT 1", 1, 2.5)
    assert fake.closed_cursors == 1


LEAKY = ("SQL compilation error:\nObject 'WAREHOUSE_MCP.WIKIPEDIA.SECRETS' does not exist or "
         "not authorized. Account ACME-DEMO, user mcp_reader_svc, host "
         "acme-demo.snowflakecomputing.com:443, request 0a1b2c3d-1234-4abc-9def-0123456789ab.")


@pytest.mark.parametrize("error_class", [snowflake_errors.ProgrammingError,
                                         snowflake_errors.DatabaseError,
                                         snowflake_errors.OperationalError])
def test_errors_lose_account_user_host_and_ids(fake: FakeConnection,
                                               error_class: type[Exception]) -> None:
    fake.error = error_class(msg=LEAKY, errno=2003, sqlstate="02000",
                             sfqid="01b2c3d4-0000-1111-0000-000000000001")
    with pytest.raises(BackendError) as excinfo:
        SnowflakeBackend(CONNECTION).execute("SELECT 1", 1, 5)
    message = str(excinfo.value)
    assert message.startswith("SQL compilation error: Object")
    for leak in ("acme", "ACME", "mcp_reader_svc", "snowflakecomputing", "0a1b2c3d",
                 "01b2c3d4", "02000", "2003"):
        assert leak not in message
    assert "SECRETS" not in scrub_error(message)  # the service then drops quoted names


def test_connect_failures_are_scrubbed(monkeypatch: pytest.MonkeyPatch, key_file: Path) -> None:
    def refuse(**kwargs: Any) -> None:
        raise snowflake_errors.OperationalError(
            msg="Failed to connect to acme-demo.snowflakecomputing.com: JWT token is invalid. "
                "[0a1b2c3d-1234-4abc-9def-0123456789ab]", errno=250001)

    monkeypatch.setattr(connector, "connect", refuse)
    with pytest.raises(BackendError) as excinfo:
        SnowflakeBackend(CONNECTION)
    message = str(excinfo.value)
    assert message.startswith("Could not connect to Snowflake: ") and "JWT token" in message
    assert "acme" not in message and "0a1b2c3d" not in message


def test_table_columns_are_lower_cased(fake: FakeConnection) -> None:
    fake.columns = ["name", "type", "kind"]
    fake.rows = [("EVENT_ID", "VARCHAR(16777216)", "COLUMN"),
                 ("EVENT_TIME", "TIMESTAMP_NTZ(9)", "COLUMN")]
    columns = SnowflakeBackend(CONNECTION).table_columns("WAREHOUSE_MCP.WIKIPEDIA.edits")
    assert columns == {"event_id": "VARCHAR(16777216)", "event_time": "TIMESTAMP_NTZ(9)"}
    assert fake.executed[0][0] == "DESCRIBE TABLE WAREHOUSE_MCP.WIKIPEDIA.edits"


def test_a_missing_table_is_a_config_mismatch_not_a_crash(fake: FakeConnection) -> None:
    fake.error = snowflake_errors.ProgrammingError(msg="Object does not exist", errno=2003)
    assert SnowflakeBackend(CONNECTION).table_columns("WAREHOUSE_MCP.WIKIPEDIA.nope") == {}


@pytest.mark.parametrize("table", ["edits; DROP TABLE x", '"Quoted".a.edits', "a.b.c.d", "",
                                   "edits", "WIKIPEDIA.edits"])
def test_table_names_from_the_config_are_validated(fake: FakeConnection, table: str) -> None:
    with pytest.raises(BackendError):
        SnowflakeBackend(CONNECTION).table_columns(table)
    assert fake.executed == []
