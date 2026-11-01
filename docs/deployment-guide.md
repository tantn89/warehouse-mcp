# Deployment guide

Commands to install, run and test. For rollout strategy at a customer see
[deployment-playbook.md](deployment-playbook.md). Requires Python 3.11+.

## Install

```bash
pip install warehouse-mcp                 # DuckDB only
pip install "warehouse-mcp[postgres]"     # + psycopg
pip install "warehouse-mcp[bigquery]"     # + google-cloud-bigquery
pip install -e ".[dev,eval]"              # from a checkout: tests, lint, agent eval
```

Package-index availability is not verified here. Until a release is published, run from git:
`uvx --from git+https://github.com/tantn89/warehouse-mcp warehouse-mcp --demo`. Add `--with psycopg[binary]`
(or the BigQuery client) for the other backends.

## Run with stdio (local agent)

`--demo` serves the bundled Wikipedia snapshot from DuckDB (built once into the user cache dir).
`--config` serves your own YAML. stdio uses the config's `default_role`; override with `--role`.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "warehouse-demo": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tantn89/warehouse-mcp", "warehouse-mcp", "--demo"]
    },
    "warehouse": {
      "command": "warehouse-mcp",
      "args": ["--config", "/abs/path/config.yaml", "--role", "analyst"],
      "env": { "WAREHOUSE_MCP_PII_SALT": "<16-128 letters or digits>" }
    }
  }
}
```

Claude Code:

```bash
claude mcp add warehouse-demo -- warehouse-mcp --demo
claude mcp add warehouse -- warehouse-mcp --config /abs/path/config.yaml
```

## Run over HTTP with bearer keys

Each role in the config names an env var in `api_key_env`; the bearer key sent decides the role.
The demo roles use these variables; a role whose variable is unset cannot authenticate, and at least one must be set.

```bash
export WAREHOUSE_MCP_KEY_ANALYST=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export WAREHOUSE_MCP_KEY_ENWIKI_PARTNER=...
export WAREHOUSE_MCP_KEY_TRUST_AND_SAFETY=...
warehouse-mcp --demo --transport http --host 127.0.0.1 --port 8000
```

The endpoint is `http://127.0.0.1:8000/mcp`. Clients send `Authorization: Bearer <key>`:

```bash
claude mcp add --transport http warehouse http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $WAREHOUSE_MCP_KEY_ANALYST"
```

`--role` is rejected with HTTP. The server speaks plain HTTP; put TLS in front for anything beyond loopback.

## Postgres (local container)

```bash
docker compose up -d --wait          # Postgres 16 on 127.0.0.1:55432, roles from docker/postgres-init.sql
python scripts/load_snapshot.py      # loads schema "wikipedia" as warehouse_owner
export WAREHOUSE_MCP_POSTGRES_DSN=postgresql://mcp_reader:local-only-reader-password@127.0.0.1:55432/warehouse
export WAREHOUSE_MCP_SCHEMA=wikipedia
warehouse-mcp --config examples/postgres.yaml
```

The DSN env var name is `connection.dsn_env` (default `WAREHOUSE_MCP_POSTGRES_DSN`). Tables resolve
as `${WAREHOUSE_MCP_SCHEMA}.<table>`. All passwords in the compose file are local-only placeholders.
For a real database, create a reader with `SELECT` only, as `docker/postgres-init.sql` does.

## BigQuery

```bash
gcloud auth application-default login
python scripts/load_snapshot.py --target bigquery --project my-project --dataset wikipedia --location US
export GOOGLE_CLOUD_PROJECT=my-project
export WAREHOUSE_MCP_SCHEMA=my-project.wikipedia
warehouse-mcp --config examples/bigquery.yaml
```

Credentials are Application Default Credentials; nothing is stored in the config. Grant the identity
`roles/bigquery.dataViewer` on the dataset and `roles/bigquery.jobUser` on the project. Every query
is dry-run first and refused above `connection.maximum_bytes_billed` (default 1,000,000,000; the
minimum accepted is 10,485,760). This path has not been run against a live project yet.
Measured: 15/15 parity cases identical to DuckDB, about $0.00006 per demo query, and a 4.65 GB scan refused by the cap before billing ([bigquery-run.md](bigquery-run.md)).

## Audit log and PII salt

| Setting | How | Default |
|---------|-----|---------|
| Audit file | `audit_log:` in the config (the demo reads `WAREHOUSE_MCP_AUDIT_LOG`) | stderr when empty |
| PII salt | env `WAREHOUSE_MCP_PII_SALT`, 16-128 letters or digits | Random per process, so pseudonyms change on restart |

Audit files matching `audit*.jsonl` are git-ignored. The log never contains result values or the salt.

## Tests and checks

```bash
pip install -e ".[dev,eval]"
ruff check src tests scripts
mypy
pytest                               # Postgres tests skip without a DSN
```

To include the Postgres parity and red-team runs, start the container and load the snapshot (above),
then set `WAREHOUSE_MCP_POSTGRES_DSN` to the reader DSN before `pytest`. CI does the same with a
service container (`.github/workflows/ci.yml`). `tests/test_evaluation.py` needs no API key.

## Agent eval

Live runs spend model credits. Results are not published yet.
Baseline (Claude Haiku 5.5, 26 cases x 3 trials): 100% accuracy (78/78), $0.0016 per question, p50/p95 latency 3.3/6.3 s, 3.0 tool calls per question; all 10 rejected tool calls were self-corrected from the error message ([evals/baseline.json](../evals/baseline.json)).

Local (needs `ANTHROPIC_API_KEY` and the `eval` extra; `WAREHOUSE_MCP_EVAL_MODEL` picks the model, default `claude-haiku-5-5`):

```bash
python scripts/run_agent_eval.py --repeats 1 --only total-changes bot-share-overall
python scripts/run_agent_eval.py --summarize path/to/report.json
regressgate run --out head.json && regressgate compare evals/baseline.json head.json
```

GitHub: the `eval-gate` workflow runs on demand (manual dispatch, or a PR labelled `run-evals`) and needs an
`ANTHROPIC_API_KEY` repository secret.

| Mode | Does | Next step |
|------|------|-----------|
| `baseline` | Runs the suite, uploads the `eval-report` artifact | Commit the report as `evals/baseline.json` (not present yet) |
| `gate` | Runs the suite and compares it with `evals/baseline.json`; fails without it | Thresholds (`alpha`, `max_mean_cost_usd`, `max_p95_latency_s`) are in `regressgate.yaml` |
