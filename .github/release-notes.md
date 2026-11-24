Snowflake joins DuckDB, Postgres and BigQuery. Install with `pip install "warehouse-mcp[snowflake]"`.

- **Snowflake backend:**
  - key-pair auth for a `TYPE=SERVICE` reader user, with the private key read from a file named
    by an env var;
  - fully qualified tables only, with no default database or schema in the session;
  - statement, session, user and warehouse timeouts;
  - an expired session is replaced once on the next call.
- **Least-privilege setup:** [`docker/snowflake-setup.sql`](https://github.com/tantn89/warehouse-mcp/blob/main/docker/snowflake-setup.sql).
  - reader and owner roles;
  - an XS warehouse that suspends after 60 s;
  - a resource monitor;
  - unloads to internal stages switched off for the reader.

  A snapshot loader: `scripts/load_snapshot.py --target snowflake`.
- **Guard:**
  - refuses Snowflake table functions and row generators, session and account metadata
    functions, and time travel;
  - refuses table clauses the policy rewrite would otherwise drop silently: `SAMPLE` and
    `TABLESAMPLE`, `PIVOT` and `UNPIVOT`, joins nested in parentheses, alias column lists.
- **Live evidence** ([docs/snowflake-run.md](https://github.com/tantn89/warehouse-mcp/blob/main/docs/snowflake-run.md)), from a real Snowflake
  account:
  - the 15 parity queries match DuckDB, compared at Snowflake's 6-decimal scale;
  - the red-team suite's 151 cases leak nothing;
  - writes fail on grants alone;
  - unloads to `@~` are refused;
  - the statement timeout cancels on the server;
  - a server idle for 4 h 20 min keeps working;
  - the run cost 0.12 credits.

  The live run found and fixed one bug: after a long idle period the connector raises
  `ReauthenticationRequest` with no error code, which the backend now treats as an expired
  session.
