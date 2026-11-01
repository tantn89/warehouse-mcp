"""Append-only audit trail: one JSON line per tool call, allowed or denied.

Records what was asked (the logical SQL and its hash), by which role, what the guard decided
and what came back (rows, bytes, time). It never records result values or the physical SQL,
which contains the pseudonym salt and the row filters.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str | None) -> None:
        self._path = Path(path) if path else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, *, role: str, tool: str, decision: str, sql: str | None = None,
               **fields: Any) -> None:
        event: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "role": role, "tool": tool, "decision": decision,
        }
        if sql is not None:
            event["sql"] = sql
            event["sql_sha256"] = hashlib.sha256(sql.encode()).hexdigest()[:16]
        event.update(fields)
        line = json.dumps(event, default=str)
        with self._lock:
            if self._path:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            else:
                # stdout carries the MCP protocol on stdio; the log goes to stderr.
                print(line, file=sys.stderr, flush=True)
