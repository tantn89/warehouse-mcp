"""The Snowflake loader refuses to run as the reader, by name and by the session's real role."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("snowflake.connector")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import load_snapshot_snowflake as loader


class RoleCursor:
    def __init__(self, role: str) -> None:
        self._role = role

    def execute(self, sql: str) -> None:
        assert sql == "SELECT CURRENT_ROLE()"

    def fetchone(self) -> tuple[str]:
        return (self._role,)

    def close(self) -> None:
        return None


class RoleConnection:
    def __init__(self, role: str) -> None:
        self._role = role

    def cursor(self) -> RoleCursor:
        return RoleCursor(self._role)


@pytest.mark.parametrize(("user", "role"), [("MCP_READER_SVC", "WAREHOUSE_OWNER"),
                                            ("mcp_reader_svc", "WAREHOUSE_OWNER"),
                                            ("WAREHOUSE_OWNER_SVC", "mcp_reader")])
def test_the_reader_user_or_role_is_refused_before_connecting(
        monkeypatch: pytest.MonkeyPatch, user: str, role: str) -> None:
    def connect(**kwargs: Any) -> None:
        raise AssertionError("must not connect")

    monkeypatch.setattr(loader.snowflake.connector, "connect", connect)
    with pytest.raises(SystemExit, match="never as MCP_READER_SVC or MCP_READER"):
        loader.load_snowflake("acme-demo", user, role, "MCP_WH", "WAREHOUSE_MCP", "WIKIPEDIA")


@pytest.mark.parametrize("current", ["MCP_READER", "PUBLIC", "SYSADMIN"])
def test_a_session_on_another_role_is_refused(current: str) -> None:
    with pytest.raises(SystemExit, match="refusing to load"):
        loader.check_role(RoleConnection(current), "WAREHOUSE_OWNER")  # type: ignore[arg-type]


def test_the_owner_session_passes() -> None:
    loader.check_role(RoleConnection("WAREHOUSE_OWNER"), "warehouse_owner")  # type: ignore[arg-type]
