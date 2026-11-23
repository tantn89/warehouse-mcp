Security fix release. Upgrade from 0.1.0: `pip install -U warehouse-mcp` or `uvx warehouse-mcp@0.1.1`.

**Fixed: a CTE named like a dataset but in a different case could skip the policy rewrite.**
On engines that treat quoted identifiers as case-sensitive (Postgres, and Snowflake), a query such
as `WITH "EDITS" AS (...) SELECT editor FROM edits` was treated as reading the CTE. The engine
resolves the unquoted `edits` to the real table, so the outer reference was left unrewritten. Where
that table was reachable without a schema prefix (for example tables in Postgres `public`, or a
search path that includes the dataset schema), an agent could read raw columns, outside the
role's row filter and its masked personal data.

- CTE names and table references are now compared with each engine's own identifier rules.
- A CTE whose name differs from a dataset only by case or quoting is refused.
- DuckDB, and the bundled `--demo`, were not affected: DuckDB identifiers are case-insensitive.
  The Postgres setup in `docker/postgres-init.sql` was not exposed either: its tables live in a
  schema outside the default search path.
- New guard and red-team tests cover the case on DuckDB, Postgres, BigQuery and Snowflake
  dialects.

Also: a red-team test now accepts newer sqlglot parsing `TABLE edits` as a policy-guarded read.
