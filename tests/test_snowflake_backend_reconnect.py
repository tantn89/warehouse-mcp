"""A Snowflake session that expired while the server sat idle is replaced, once, on demand."""

from __future__ import annotations

import threading
from typing import Any

import pytest

connector = pytest.importorskip("snowflake.connector")

from snowflake.connector import errors as snowflake_errors  # noqa: E402

# key_file sets the key env var to a placeholder file; used through usefixtures.
from test_snowflake_backend import CONNECTION, FakeConnection, key_file  # noqa: E402, F401
from warehouse_mcp.backends import BackendError  # noqa: E402
from warehouse_mcp.backends.snowflake_backend import SnowflakeBackend  # noqa: E402


def _sessions(monkeypatch: pytest.MonkeyPatch, *conns: FakeConnection) -> list[FakeConnection]:
    """Each connect() hands out the next connection; the list shows which were never used."""
    pending = list(conns)

    def connect(**kwargs: Any) -> FakeConnection:
        return pending.pop(0)

    monkeypatch.setattr(connector, "connect", connect)
    return pending


def _fresh() -> FakeConnection:
    conn = FakeConnection()
    conn.columns, conn.rows = ["X"], [(1,)]
    return conn


pytestmark = pytest.mark.usefixtures("key_file")


@pytest.mark.parametrize(("errno", "where"), [(390112, "execute"), (390114, "execute"),
                                              (250002, "cursor")])
def test_an_expired_or_closed_session_is_replaced_once(
        monkeypatch: pytest.MonkeyPatch, errno: int, where: str) -> None:
    stale, fresh = FakeConnection(), _fresh()
    error = snowflake_errors.ProgrammingError(msg="Authentication token has expired.",
                                              errno=errno)
    if where == "cursor":
        stale.cursor_error = error
    else:
        stale.error = error
    pending = _sessions(monkeypatch, stale, fresh)
    backend = SnowflakeBackend(CONNECTION)
    assert backend.execute("SELECT 1", 5, 5).rows == [(1,)]
    assert stale.closed and pending == []
    assert backend.execute("SELECT 2", 5, 5).rows == [(1,)]  # the new session is kept
    assert [sql for sql, _ in fresh.executed] == ["SELECT 1", "SELECT 2"]


@pytest.mark.skipif(not hasattr(snowflake_errors, "ReauthenticationRequest"),
                    reason="this connector has no ReauthenticationRequest")
def test_a_session_the_connector_cannot_renew_is_replaced(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # What a live account raised after 4 h 20 min idle: no errno, only the class.
    stale, fresh = FakeConnection(), _fresh()
    stale.error = snowflake_errors.ReauthenticationRequest(
        msg="Master token can never be renewed - full re-authentication required")
    pending = _sessions(monkeypatch, stale, fresh)
    assert SnowflakeBackend(CONNECTION).execute("SELECT 1", 5, 5).rows == [(1,)]
    assert stale.closed and pending == []


def test_table_columns_reconnect_too(monkeypatch: pytest.MonkeyPatch) -> None:
    stale, fresh = FakeConnection(), FakeConnection()
    stale.error = snowflake_errors.ProgrammingError(msg="Session expired.", errno=390112)
    fresh.columns, fresh.rows = ["name", "type"], [("WIKI", "VARCHAR")]
    _sessions(monkeypatch, stale, fresh)
    assert SnowflakeBackend(CONNECTION).table_columns("A.B.EDITS") == {"wiki": "VARCHAR"}


def test_other_errors_are_reported_without_reconnecting(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConnection()
    conn.error = snowflake_errors.ProgrammingError(msg="SQL compilation error", errno=1003)
    pending = _sessions(monkeypatch, conn, _fresh())
    with pytest.raises(BackendError, match="SQL compilation error"):
        SnowflakeBackend(CONNECTION).execute("SELECT", 1, 5)
    assert len(pending) == 1 and not conn.closed


def test_a_failing_new_session_is_reported_not_retried_again(
        monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = FakeConnection(), FakeConnection()
    for conn in (first, second):
        conn.error = snowflake_errors.ProgrammingError(msg="Session expired.", errno=390112)
    pending = _sessions(monkeypatch, first, second, _fresh())
    with pytest.raises(BackendError, match="Session expired"):
        SnowflakeBackend(CONNECTION).execute("SELECT 1", 1, 5)
    assert len(pending) == 1  # one retry only


def test_concurrent_calls_on_a_dead_session_reconnect_once(monkeypatch: pytest.MonkeyPatch) -> None:
    stale, fresh = FakeConnection(), _fresh()
    stale.error = snowflake_errors.ProgrammingError(msg="Session expired.", errno=390112)
    both_failing = threading.Barrier(2, timeout=5)
    stale.on_execute = both_failing.wait  # both calls hit the dead session before either retries
    pending = _sessions(monkeypatch, stale, fresh, _fresh())
    backend = SnowflakeBackend(CONNECTION)
    results: list[object] = []
    threads = [threading.Thread(target=lambda: results.append(backend.execute("SELECT 1", 5, 5)))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(results) == 2 and len(pending) == 1 and len(fresh.executed) == 2
