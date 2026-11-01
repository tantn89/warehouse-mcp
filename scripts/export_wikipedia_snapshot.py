"""Export the demo snapshot from a running realtime-lakehouse stack.

The demo warehouse is the output of https://github.com/tantn89/realtime-lakehouse: silver
edits and editor sessions read from Iceberg, plus the dbt gold marts. Run it inside that
stack's Dagster container, which can read both:

    docker cp scripts/export_wikipedia_snapshot.py realtime-lakehouse-dagster-webserver-1:/tmp/
    docker exec realtime-lakehouse-dagster-webserver-1 python /tmp/export_wikipedia_snapshot.py
    docker cp realtime-lakehouse-dagster-webserver-1:/tmp/wikipedia_snapshot/. \
        src/warehouse_mcp/demo/data/

Timestamps are written as naive UTC and wide integers as BIGINT, so the same Parquet files
load unchanged into DuckDB, Postgres and BigQuery. Wikipedia content is CC BY-SA 4.0.
"""

from __future__ import annotations

import os
from typing import Any

from wiki_lakehouse.orchestration.lakehouse_duckdb import (  # type: ignore[import-not-found]
    connect,
)

OUT = "/tmp/wikipedia_snapshot"

QUERIES = {
    "edits": """
        SELECT event_id, timezone('UTC', event_time) AS event_time, event_type, wiki,
               namespace, title, "user" AS editor, is_bot, is_minor, length_delta, comment
        FROM lake.silver.edits""",
    "editor_sessions": """
        SELECT wiki, "user" AS editor, timezone('UTC', session_start) AS session_start,
               timezone('UTC', session_end) AS session_end, edits, bytes_changed, pages, is_bot
        FROM lake.silver.editor_sessions""",
    "edits_per_minute": """
        SELECT timezone('UTC', event_minute) AS event_minute, wiki, edits, bot_edits,
               human_edits, new_pages, bytes_added::BIGINT AS bytes_added,
               bytes_removed::BIGINT AS bytes_removed
        FROM gold_db.gold.edits_per_minute""",
    "bot_activity_daily": "SELECT * FROM gold_db.gold.bot_activity_daily",
    "trending_articles": """
        SELECT wiki, title, editors, edits, timezone('UTC', last_edit_at) AS last_edit_at,
               trend_rank
        FROM gold_db.gold.trending_articles""",
    "editor_sessions_daily": """
        SELECT session_date, wiki, is_bot, sessions, avg_edits_per_session,
               median_session_minutes, bytes_changed::BIGINT AS bytes_changed
        FROM gold_db.gold.editor_sessions_daily""",
}


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    con: Any = connect()
    con.execute("ATTACH '/gold/lakehouse.duckdb' AS gold_db (READ_ONLY)")
    for name, sql in QUERIES.items():
        path = f"{OUT}/{name}.parquet"
        con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
        print(f"{name}: {rows} rows, {os.path.getsize(path) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
