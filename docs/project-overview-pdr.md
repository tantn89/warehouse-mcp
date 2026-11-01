# Project overview and requirements

warehouse-mcp is a Python [MCP](https://modelcontextprotocol.io) server that gives AI agents
governed, read-only access to DuckDB, Postgres and BigQuery. The deployer declares what an agent
may see in one YAML file; the server enforces it on every call.

## Problem

Pointing an agent at a customer warehouse fails in four predictable ways.

| Risk | What goes wrong |
|------|-----------------|
| Data leak | The agent reads a table or row the asking user was never meant to see. |
| PII exposure | Names, emails and phone numbers reach the model provider and the chat transcript. |
| Runaway cost | One careless `SELECT *` or cross join on a pay-per-scan warehouse spends real money. |
| Prompt injection | Text inside the data (or the user prompt) talks the agent into writing, exfiltrating or escalating. |

Prompting the agent to "be careful" is not a control. Controls must sit outside the model and hold
when the model is wrong or hostile. See [system-architecture.md](system-architecture.md) for how.

## Goals

- An agent answers business questions from curated metrics and declared datasets, with no write path.
- Different callers see different datasets, rows and identities, decided by a credential and not by the prompt.
- Person-identifying values never leave the warehouse in clear text for uncleared roles, while counts and joins stay exact.
- Every call, allowed or refused, leaves an audit record that never contains result values.
- Answer quality is measurable and gated in CI, not asserted.
- A forward-deployed engineer can roll this out at a customer following [deployment-playbook.md](deployment-playbook.md).

## Non-goals

| Non-goal | Reason |
|----------|--------|
| Writes of any kind (DML, DDL, `COPY`, `SET`) | The product is the read-only guarantee. |
| Snowflake backend | Roadmap, see [project-roadmap.md](project-roadmap.md). |
| OAuth authorization server | HTTP uses static bearer keys; the server is a resource server only, and OAuth modes are roadmap. |
| UI or dashboard | The surface is MCP tools; audit review uses the JSONL file. |
| Reversible masking or tokenization vault | Pseudonyms are one-way by design. |

## Requirements

Each requirement links the code that implements it and the test that proves it. A requirement
with no test is marked.

| # | Requirement | Acceptance criteria | Evidence |
|---|-------------|---------------------|----------|
| R1 | Only one read query per call | Multiple statements, DML, DDL, `SELECT INTO`, row locks, `WITH RECURSIVE`, non-literal `LIMIT` are refused before the warehouse is reached. | `src/warehouse_mcp/sql_guard.py`; `test_writes_and_side_effects_are_rejected`, `test_writes_and_smuggled_statements_are_rejected` |
| R2 | Only declared datasets and columns are queryable | Physical tables, schema-qualified names, catalogs, table functions and look-alike Unicode names are refused; undeclared columns do not exist. | `sql_guard.py` (`_dataset_references`, `policy_source`); `test_ungranted_and_physical_tables_are_rejected`, `test_undeclared_columns_do_not_exist` |
| R3 | No file, network or session access | Unknown (parser-opaque) functions are denied by default; file readers, sleeps, `current_user` refused. | `sql_guard.py` (`_check_functions`); `test_file_network_and_system_functions_are_rejected` |
| R4 | Roles grant datasets and mandatory row filters | A role never sees ungranted datasets; its row filter holds inside CTEs, subqueries and joins, and errors are no oracle for hidden rows. | `config.py` (`Role`); `test_row_filter_applies_inside_ctes_and_subqueries`, `test_row_filter_cannot_be_escaped`, `test_errors_are_no_oracle_for_rows_outside_the_filter` |
| R5 | PII is pseudonymized at the source | Uncleared roles get salted `pii_<12 hex>` values; `count(DISTINCT)` and joins stay exact; name probes match nothing; a cleared role sees real values. | `sql_guard.py` (`pseudonym`); `test_pii_is_pseudonymized_but_distinct_counts_stay_exact`, `test_name_probes_match_nothing`, `test_cleared_role_is_the_positive_control` |
| R6 | Conditional PII | `pii_when` masks only rows matching a predicate (user-page titles). | `config.py` (`Column.pii_when`); `test_own_user_page_titles_do_not_relink_pseudonyms` |
| R7 | Free text is scanned on the way out | Emails redacted in every string; phone numbers in `free_text` columns; cells and results are size-capped. | `redaction.py`, `service.py`; `test_long_strings_do_not_stall_redaction`, `test_a_single_huge_cell_respects_the_result_cap`. Email/phone replacement values have no dedicated test. |
| R8 | Rows, time and bytes are bounded | `max_rows`, `timeout_s`, `max_result_chars`, `max_cell_chars`; BigQuery dry-run plus `maximum_bytes_billed`. | `config.py` (`Limits`); `test_limit_is_capped_and_truncation_detectable`, `test_heavy_queries_are_bounded_by_rows_and_time`, `test_statement_timeout_cancels_on_the_server`. BigQuery cap: no automated test. |
| R9 | Backends enforce read-only independently of the guard | DuckDB read-only with external access off; Postgres read-only transaction, statement timeout, SELECT-only grants. | `backends/*.py`, `docker/postgres-init.sql`; `test_backend_refuses_writes_and_server_files`, `test_reader_grants_refuse_writes_in_a_read_write_session` |
| R10 | Identical answers across engines | The same governed questions return the same rows on DuckDB and Postgres. | `test_duckdb_and_postgres_agree` (needs `WAREHOUSE_MCP_POSTGRES_DSN`) |
| R11 | HTTP callers are authenticated by key and the key decides the role | One bearer key per role from env vars; constant-time comparison; no key, no access. | `server.py` (`BearerKeyVerifier`). No automated test of the HTTP path. |
| R12 | Audit without secrets | One JSON line per call with role, decision, logical SQL and its hash; never the salt, physical SQL or result values. | `audit.py`; `test_audit_log_never_holds_the_salt`, `test_every_call_is_audited_without_result_values` |
| R13 | Tool errors tell the agent the next step | Rejections name the available datasets, metrics or columns. | `test_rejections_are_tool_errors_with_a_next_step` |
| R14 | Answer quality and governance are measured | Deterministic ground-truth grading, no LLM judge; governance cases (leak, write, file access, row policy) in the same suite; CI gate on accuracy, cost and latency. | `evals/cases.jsonl`, `regressgate.yaml`, `.github/workflows/eval-gate.yml`, `tests/test_evaluation.py` |

## Evidence status

- Guard, redaction, row policy, pseudonyms, audit: proven by tests on DuckDB, and on Postgres in CI.
- BigQuery: measured on a live project: 15/15 parity cases identical to DuckDB, and the byte cap refuses a 4.65 GB scan before billing ([bigquery-run.md](bigquery-run.md)). The live run is not part of CI (needs credentials).
- Baseline (Claude Haiku 5.5, 26 cases x 3 trials): 100% accuracy (78/78), $0.0016 per question, p50/p95 latency 3.3/6.3 s, 3.0 tool calls per question; all 10 rejected tool calls were self-corrected from the error message ([evals/baseline.json](../evals/baseline.json)).

## Demo dataset

The bundled demo serves a Wikipedia edits snapshot (CC BY-SA 4.0) produced by a streaming
lakehouse project; `src/warehouse_mcp/demo/wikipedia.yaml` is the reference semantic model.
