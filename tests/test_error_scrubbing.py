"""Engine errors can quote data values; they must not reach the agent or the audit log."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_settings
from warehouse_mcp.audit import AuditLog
from warehouse_mcp.backends import BackendError, open_backend
from warehouse_mcp.service import WarehouseService, scrub_error


def test_scrub_removes_quoted_values_and_contacts() -> None:
    message = ("Conversion Error: Could not convert string 'mail me a@b.com' to INT32 "
               "near \"comment\" call +1 (415) 555-0100")
    scrubbed = scrub_error(message)
    assert "a@b.com" not in scrubbed and "555-0100" not in scrubbed
    assert "Could not convert string '…' to INT32" in scrubbed


def test_scrub_caps_length() -> None:
    assert len(scrub_error("x" * 5000)) <= 301


def test_failed_cast_leaks_nothing_to_the_agent_or_the_audit_log(demo_db: Path,
                                                                tmp_path: Path) -> None:
    settings = make_settings(str(demo_db))
    audit_file = tmp_path / "audit.jsonl"
    service = WarehouseService(settings, open_backend(settings.connection),
                               AuditLog(str(audit_file)), pii_salt="0123456789abcdef")
    # A cleared role reads raw comments, so the cast error would quote the raw text.
    with pytest.raises(BackendError) as caught:
        service.run_sql(settings.role("admin"), "SELECT CAST(comment AS INTEGER) FROM edits")
    for text in (str(caught.value), audit_file.read_text(encoding="utf-8")):
        assert "a@b.com" not in text and "fix typo" not in text and "555 0100" not in text
