First release of warehouse-mcp: governed, read-only MCP access to DuckDB, Postgres and BigQuery
for AI agents.

- **Install:** `uvx warehouse-mcp --demo` serves about 100k live Wikipedia edits with no setup.
  `--config` serves your own warehouse.
- **Guard:**
  - one read-only statement over declared datasets only;
  - a function allowlist;
  - CTE references resolved by scope;
  - every dataset rewritten into a policy subquery with declared columns, salted pseudonyms
    for personal data (conditional with `pii_when`) and the role's row filter.
- **Safeguards in each engine:**
  - DuckDB is read-only with file access disabled;
  - Postgres uses read-only transactions and grants;
  - BigQuery queries are dry-run and capped with `maximum_bytes_billed`.
- **Evidence:**
  - 104 red-team attempts per engine with 0 leaks;
  - 15 parity queries return identical results on DuckDB, Postgres and BigQuery;
  - the Claude Haiku 5.5 agent eval scored 78/78 at $0.0016 per question, gated by regressgate.
- **Fixed before release:** the red-team suite and an independent review found five gaps, all
  now covered by regression tests. Details are in the README.
