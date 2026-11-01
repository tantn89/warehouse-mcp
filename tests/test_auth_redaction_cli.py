"""HTTP bearer keys map to roles, free-text redaction replaces what it should, CLI guards."""

from __future__ import annotations

import pytest

from conftest import make_settings
from warehouse_mcp.cli import main
from warehouse_mcp.redaction import redact
from warehouse_mcp.server import BearerKeyVerifier


def _settings_with_keys():  # type: ignore[no-untyped-def]
    return make_settings(roles=[
        {"name": "analyst", "datasets": ["edits"], "api_key_env": "TEST_KEY_ANALYST"},
        {"name": "admin", "datasets": "*", "pii_clearance": True,
         "api_key_env": "TEST_KEY_ADMIN"},
        {"name": "keyless", "datasets": ["edits"]},
    ])


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_each_key_resolves_to_its_role(monkeypatch: pytest.MonkeyPatch,
                                             anyio_backend: str) -> None:
    monkeypatch.setenv("TEST_KEY_ANALYST", "analyst-secret-1")
    monkeypatch.setenv("TEST_KEY_ADMIN", "admin-secret-2")
    verifier = BearerKeyVerifier(_settings_with_keys())
    analyst = await verifier.verify_token("analyst-secret-1")
    admin = await verifier.verify_token("admin-secret-2")
    assert analyst is not None and analyst.client_id == "analyst"
    assert admin is not None and admin.client_id == "admin"
    assert await verifier.verify_token("wrong") is None
    assert await verifier.verify_token("") is None


def test_http_needs_at_least_one_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_KEY_ANALYST", raising=False)
    monkeypatch.delenv("TEST_KEY_ADMIN", raising=False)
    with pytest.raises(SystemExit):
        BearerKeyVerifier(_settings_with_keys())


@pytest.mark.parametrize(("text", "phones", "expected", "count"), [
    ("mail me at jane.doe@example.org please", False, "mail me at [email] please", 1),
    ("call +1 (415) 555-0100 or 0912 345 678", True, "call [phone] or [phone]", 2),
    ("call +1 (415) 555-0100", False, "call +1 (415) 555-0100", 0),
    ("List of films 1990 1999", False, "List of films 1990 1999", 0),
    ("revision 12345 by bot", True, "revision 12345 by bot", 0),
])
def test_redaction_replacements(text: str, phones: bool, expected: str, count: int) -> None:
    assert redact(text, phones=phones) == (expected, count)


def test_cli_refuses_role_override_over_http(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--demo", "--transport", "http", "--role", "analyst"])
    assert "bearer key decides the role" in capsys.readouterr().err


def test_cli_needs_a_config_source() -> None:
    with pytest.raises(SystemExit):
        main([])
