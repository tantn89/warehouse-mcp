# Codebase summary

Navigation only. Behavior is in the modules; reasons are in [system-architecture.md](system-architecture.md).
Python 3.11+, package `warehouse_mcp` under `src/`, entry point `warehouse-mcp` (`cli.py:main`).

## Source map

| Path | Owns |
|------|------|
| `src/warehouse_mcp/cli.py` | Arguments (`--config` or `--demo`, `--transport`, `--host`, `--port`, `--role`), startup validation, `WAREHOUSE_MCP_PII_SALT` |
| `src/warehouse_mcp/config.py` | YAML schema (pydantic, `extra="forbid"`), `${ENV}` expansion, `extends` (path or `demo:<name>`), cross-reference validation |
| `src/warehouse_mcp/server.py` | MCP tools, server `INSTRUCTIONS`, `BearerKeyVerifier`, role resolution per call |
| `src/warehouse_mcp/service.py` | `WarehouseService`: role -> guard -> backend -> cell/result caps -> redaction -> audit |
| `src/warehouse_mcp/sql_guard.py` | Parse, reject, dataset resolution, `LIMIT` cap, policy subquery rewrite, pseudonym expression |
| `src/warehouse_mcp/semantic.py` | Compiles a metrics request (metrics, dimensions, filters, time grain) to SQL as a syntax tree |
| `src/warehouse_mcp/redaction.py` | Email and phone replacement for text leaving the server |
| `src/warehouse_mcp/audit.py` | JSONL audit trail (file or stderr) |
| `src/warehouse_mcp/backends/` | `base.py` protocol and `BackendError`; `duckdb_backend.py`, `postgres_backend.py`, `bigquery_backend.py`; `__init__.py:open_backend` picks one and imports drivers lazily |
| `src/warehouse_mcp/demo/` | Bundled Wikipedia snapshot (`data/*.parquet`), the reference semantic model `wikipedia.yaml`, and the builder of the cached DuckDB file |
| `src/warehouse_mcp/evaluation/` | Agent eval: `agent_loop.py` (Claude over the in-memory MCP server), `grading.py` (answer parsing, numeric tolerance), `ground_truth.py` (the `ground_truth` check), `pricing.py`, `regressgate_target.py` (entry point named in `regressgate.yaml`) |

## Tools exposed

Registered in `server.py`; all carry read-only annotations (`test_tools_are_listed_read_only`).

- `warehouse_list_datasets`
- `warehouse_describe_dataset`
- `warehouse_query_metrics` (preferred path)
- `warehouse_run_sql`

## Other top-level files

| Path | Purpose |
|------|---------|
| `examples/postgres.yaml`, `examples/bigquery.yaml` | Demo semantic model on another backend via `extends: demo:wikipedia` |
| `docker-compose.yml`, `docker/postgres-init.sql` | Local Postgres, owner and SELECT-only reader roles |
| `scripts/load_snapshot.py` | Load the snapshot into Postgres or BigQuery |
| `scripts/run_agent_eval.py` | Run or summarize the agent eval |
| `scripts/export_wikipedia_snapshot.py` | Regenerate the Parquet snapshot from the source lakehouse stack (not needed to use the repo) |
| `evals/cases.jsonl` | Eval questions with ground-truth SQL and governance checks |
| `regressgate.yaml` | Eval target, repeats and gate thresholds |
| `.github/workflows/ci.yml`, `eval-gate.yml` | CI (lint, types, tests with Postgres, wheel contents) and the on-demand eval gate |
| `pyproject.toml` | Dependencies, extras (`postgres`, `bigquery`, `eval`, `dev`), ruff, mypy, pytest settings |

## Test map

Run with `pytest`; Postgres-gated tests skip without `WAREHOUSE_MCP_POSTGRES_DSN`.

| File | Covers | Needs |
|------|--------|-------|
| `tests/test_sql_guard.py` | Rewrite correctness executed on DuckDB, rejections, limits, dialect rendering | nothing |
| `tests/test_red_team.py` | Adversarial queries through `WarehouseService` on the demo snapshot, including regressions of the three fixed gaps | DuckDB always; Postgres when DSN set |
| `tests/test_postgres_parity.py` | DuckDB and Postgres return identical governed answers; engine-level refusals; audit holds no salt. Also exports the service builders `test_red_team.py` reuses | Postgres DSN |
| `tests/test_server.py` | End to end over the MCP protocol with an in-memory client | nothing |
| `tests/test_evaluation.py` | Grading, ground-truth resolution of every eval case, agent loop with a scripted fake model | `eval` extra (skips otherwise); no API key |
| `tests/conftest.py` | Small semantic model, restricted roles, throwaway DuckDB | nothing |

Not covered by an automated test: the HTTP bearer path (`BearerKeyVerifier`), email and phone
replacement values, the BigQuery backend against a live project, and the CLI.
