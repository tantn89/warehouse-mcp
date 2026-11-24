# Snowflake run, 2026-11-24

The demo snapshot was loaded into a Snowflake trial account (Standard edition, AWS
ap-southeast-1). The account was set up with [`docker/snowflake-setup.sql`](../docker/snowflake-setup.sql)
and loaded with `scripts/load_snapshot.py --target snowflake` as the owner user. The server
read it through [`examples/snowflake.yaml`](../examples/snowflake.yaml) as `MCP_READER_SVC`,
using key-pair auth. The account objects are dropped after the run with the teardown at the end
of the setup script.

Connector: `snowflake-connector-python` 5.2. Warehouse: one XS warehouse with auto-suspend at
60 s.

## Loaded

| Table | Rows |
|---|---|
| `edits` | 100,932 |
| `editor_sessions` | 2,419 |
| `edits_per_minute` | 4,143 |
| `bot_activity_daily` | 181 |
| `trending_articles` | 50 |
| `editor_sessions_daily` | 185 |

Each count was checked against its Parquet file by the loader.

## Same answers as DuckDB

`tests/test_snowflake_parity.py` runs the 15 cross-engine cases from
`tests/test_postgres_parity.py` and compares each result with DuckDB.

**Result: 15/15 cases identical**, with numbers compared at 6 decimal places. The cases cover:
- hand-written SQL;
- `query_metrics` at minute, hour and day grain, and with filters;
- a join on pseudonyms;
- the row-filtered `enwiki_partner` role and the PII-cleared role.

The first run differed on 3 cases, all in the 7th decimal and beyond. Snowflake computes `AVG`
and division over integers as fixed-point `NUMBER` with 6 decimals, where DuckDB returns
`DOUBLE`. For example, one per-session average for human editors came back as `3.542756` against
DuckDB's `3.542755871`. The parity test now rounds both sides to Snowflake's scale.
Everything else, including pseudonyms and timestamps, must match exactly.

Each parity query took 92–129 ms on a running warehouse.

## Red team: 0 leaks

`tests/test_red_team.py` ran its 151 cases against Snowflake: **151/151 passed**, with the same
cases as DuckDB. They cover:
- exfiltration and row-filter escapes;
- PII probes;
- file, stage and system access;
- statement smuggling and resource abuse.

They also cover the Snowflake-specific refusals:
- table functions and row generators;
- session and account metadata;
- time travel;
- table clauses that the policy rewrite would drop, such as `SAMPLE` and `PIVOT`;
- CTE names that differ from a dataset only by case.

One case behaves differently on Snowflake. Snowflake has no whole-row reference, so
`CAST(e AS VARCHAR)` over a table alias is refused by the engine as an invalid identifier. No
data comes back.

## What the engine refuses with the guard out of the way

These statements were sent straight through the connector as `MCP_READER_SVC`, skipping the SQL
guard:

| Attempt | Outcome |
|---|---|
| `INSERT`, `UPDATE`, `DELETE`, `DROP TABLE` on the data | Refused on grants |
| `CREATE TABLE`, `CREATE STAGE`, `CREATE DATABASE` | Refused on grants |
| `COPY INTO @~/...` (unload to the user's own stage) | Refused: `PREVENT_UNLOAD_TO_INTERNAL_STAGES` is set on the user |
| `SELECT SYSTEM$WAIT(20)` with a 2 s limit | Cancelled by the server well within the 15 s the test allows |

Two things are not closed by grants:
- **Temporary tables** need no `CREATE TABLE` privilege, and every user has a user stage. Both
  are private to the session or the user. The guard refuses them (`CREATE`, `PUT`, `COPY`,
  stage reads).
- **The `PUBLIC` role.** Every role inherits it. On this trial, `PUBLIC` let the reader see the
  `SNOWFLAKE`, `SNOWFLAKE_LEARNING_DB` and `SNOWFLAKE_SAMPLE_DATA` databases, and read
  `SNOWFLAKE_SAMPLE_DATA` directly. The guard reads only declared datasets, so an agent cannot
  reach them. A deployment that wants the warehouse itself to enforce this should revoke those
  grants from `PUBLIC`, or check them with `SHOW GRANTS TO ROLE PUBLIC`; the setup script ends
  with that check.

## A long-idle server keeps working

The server holds one Snowflake session and does not send keep-alive heartbeats. After a long
idle period the session expires, and the next call must open a new one.

The first live check found a bug. After 4 h 20 min idle, connector 5.2 raised
`ReauthenticationRequest` ("Master token can never be renewed") with no error code. The backend
only recognised the session-expired codes 390112 and 390114 and the closed-connection code
250002, so every call would have failed until a restart.

It is fixed: the backend now also recognises that exception, and a unit test covers it. A
second 4 h 20 min idle run confirmed the fix: the second query opened a new session and
succeeded.

## Cost

The whole run used **0.12 credits** on the XS warehouse, according to Snowsight Cost Management
on the day. That covers loading, three full live test runs and two idle checks. The
`MCP_MONITOR` resource monitor was set to suspend the warehouse at 5 credits.
