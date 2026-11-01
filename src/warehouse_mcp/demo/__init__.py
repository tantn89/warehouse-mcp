"""The bundled demo: a Wikipedia edits snapshot served from a local DuckDB file.

The package ships Parquet files. On first use they are loaded into a DuckDB database in the
user cache directory, because the server opens DuckDB with file access disabled and so cannot
read Parquet at query time. Building it is the only step that reads files.
"""

from __future__ import annotations

import os
import uuid
from importlib import resources
from pathlib import Path

import duckdb

from warehouse_mcp import __version__

TABLES = ("edits", "editor_sessions", "edits_per_minute", "bot_activity_daily",
          "trending_articles", "editor_sessions_daily")


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    return Path(base or Path.home() / ".cache") / "warehouse-mcp"


def build_demo_database(target: Path) -> Path:
    """Load the bundled Parquet snapshot into a DuckDB file (idempotent, atomic)."""
    data = resources.files("warehouse_mcp.demo") / "data"
    target.parent.mkdir(parents=True, exist_ok=True)
    # A private temporary name per process: two clients starting `--demo` at once on an empty
    # cache each build their own file, and the atomic rename lets the last one win cleanly.
    tmp = target.with_name(f"{target.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.building")
    con = duckdb.connect(str(tmp))
    try:
        for table in TABLES:
            with resources.as_file(data / f"{table}.parquet") as parquet:
                con.execute(f"CREATE TABLE main.{table} AS SELECT * FROM read_parquet(?)",
                            [str(parquet)])
    finally:
        con.close()
    os.replace(tmp, target)
    return target


def demo_config_path() -> Path:
    """The demo config, with WAREHOUSE_MCP_DEMO_DB pointing at a built database."""
    database = _cache_dir() / f"wikipedia-{__version__}.duckdb"
    if not database.is_file():
        build_demo_database(database)
    os.environ.setdefault("WAREHOUSE_MCP_DEMO_DB", str(database))
    return Path(str(resources.files("warehouse_mcp.demo") / "wikipedia.yaml"))
