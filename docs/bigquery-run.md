# BigQuery run, 2026-11-01

The demo snapshot was loaded into a BigQuery dataset in a personal project, location US, with
`scripts/load_snapshot.py --target bigquery`. It was served through `examples/bigquery.yaml`
with Application Default Credentials. The dataset was deleted afterwards.

## Same answers as DuckDB

`tests/test_bigquery_parity.py` runs the 15 cross-engine cases from
`tests/test_postgres_parity.py` and compares each result with DuckDB.

**Result: 15/15 cases identical.** The cases cover hand-written SQL, `query_metrics` at minute,
hour and day grain, filters, a join on pseudonyms, the row-filtered `enwiki_partner` role and
the PII-cleared role.

The first run found one bug: the guard wrote time-range literals as `DATETIME`, and BigQuery
refused to compare them with a `TIMESTAMP` column. It is fixed. On BigQuery the literals are now
written as instants.

## Cost per query

Bytes scanned come from uncached dry runs. BigQuery bills at least 10 MB for each table read.

| Query | Bytes scanned | Billed (10 MB minimum) |
|---|---|---|
| `SELECT wiki, count(*) FROM edits GROUP BY wiki` | 544 KB | 10 MB |
| `SELECT count(DISTINCT editor) FROM edits WHERE NOT is_bot` | 554 KB | 10 MB |
| `SELECT * FROM edits` | 9.1 MB | 10 MB |
| `SELECT wiki, sum(edits) FROM edits_per_minute GROUP BY wiki` | 29 KB | 10 MB |

At $6.25 per TiB on demand, one demo query costs about $0.00006. Typical latency was 1.2–1.9 s
per query, including the dry run. Repeated queries hit BigQuery's result cache and were billed
0 bytes.

## The cost cap on a multi-terabyte table

A dataset entry pointed at the public table `bigquery-public-data.wikipedia.pageviews_2025`.
The cap was `maximum_bytes_billed: 1000000000` (1 GB).

| Agent query | Outcome |
|---|---|
| Top titles over the whole table, no time filter | Refused by BigQuery: the table requires a partition filter on `datehour` |
| Top English titles in one hour | Allowed: 870 MB scanned, under the cap |
| Views per wiki for one full day | **Refused by the server before running:** "Query would scan 4.65 GB, above the 1.00 GB cap. Select fewer columns or filter on the time column." |

The refusal comes from the free dry run, so nothing was billed. If a dry-run estimate were
wrong, `maximum_bytes_billed` on the real job would still make BigQuery reject it.
