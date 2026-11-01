# warehouse-mcp

**Give AI agents your warehouse without giving them your warehouse.**

An MCP server that lets Claude, Cursor or any MCP client query DuckDB, Postgres or BigQuery
through a governed, read-only layer:
- a semantic model, so agents ask in business terms;
- a SQL guard that an agent cannot talk into writing, reading files or leaving its lane;
- per-role row policies;
- pseudonymized personal data;
- an audit log;
- a cost cap.

Every claim below is backed by a test or a measured run.

<!-- mcp-name: io.github.tantn89/warehouse-mcp -->

![ci](https://github.com/tantn89/warehouse-mcp/actions/workflows/ci.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)
![mcp](https://img.shields.io/badge/MCP-2026--07--28-purple)
![license](https://img.shields.io/badge/license-MIT-green)

| What was checked | Result | Evidence |
|---|---|---|
| 104 bypass attempts (exfiltration, row-filter escapes, PII probes, file and system access, statement smuggling, resource abuse) | **0 leaks on DuckDB and on Postgres** | [`tests/test_red_team.py`](tests/test_red_team.py), run in CI |
| Same 15 queries on three engines | **Identical results** on DuckDB, Postgres and BigQuery | [parity tests](tests/test_postgres_parity.py), [BigQuery run](docs/bigquery-run.md) |
| Writes with the SQL guard bypassed | Refused by the engine itself: read-only transaction, then grants | [`test_postgres_parity.py`](tests/test_postgres_parity.py) |
| A careless agent query on a multi-terabyte table | Refused before billing: *"would scan 4.65 GB, above the 1.00 GB cap"* | [BigQuery run](docs/bigquery-run.md) |
| Agent answers over MCP, graded against SQL ground truth | <!-- eval results: pending first baseline run --> 26 cases, gated by [regressgate](https://github.com/tantn89/regressgate) | [`evals/cases.jsonl`](evals/cases.jsonl) |

---

## Try it in one line

The bundled demo serves about 100k live Wikipedia edits, captured by
[realtime-lakehouse](https://github.com/tantn89/realtime-lakehouse), from a local DuckDB file.
No keys are needed.

```json
{
  "mcpServers": {
    "warehouse": { "command": "uvx", "args": ["warehouse-mcp", "--demo"] }
  }
}
```

Put that in Claude Desktop's config, or run `claude mcp add warehouse -- uvx warehouse-mcp --demo`.
Then ask *"Which wikis had the most bot activity, and how many distinct editors did each have?"*

What the agent sees, taken from real tool calls on the demo:

```text
warehouse_query_metrics(dataset="edits", metrics=["edits","editors","bot_share"], dimensions=["wiki"])
  wiki          edits   editors  bot_share
  enwiktionary  41594   46       0.994
  commonswiki   16053   224      0.849
  enwiki         9576   1401     0.489

warehouse_run_sql("SELECT editor, count(*) AS edits FROM edits GROUP BY editor ORDER BY edits DESC")
  pii_6b19c4a15ccf  41335        <- a pseudonym: counts and joins are exact, the name never leaves
  pii_6a8d5fd6c44b   9638

warehouse_run_sql("SELECT * FROM edits; DROP TABLE edits")
  Error: Send exactly one statement; multiple statements are not allowed.
warehouse_run_sql("SELECT pg_read_file('/etc/passwd') FROM edits")
  Error: Function pg_read_file() is not allowed. Use standard SQL functions (...)
```

## How it works

```mermaid
flowchart LR
    A[Agent] -->|MCP: stdio, or HTTP + bearer key| T[4 read-only tools]
    T --> R[Role<br/>from the key]
    R --> G[SQL guard<br/>sqlglot, backend dialect]
    G -->|rewrite each dataset into| P[Policy subquery<br/>declared columns only<br/>salted pseudonyms<br/>role row filter]
    P --> B[(Backend safeguards<br/>DuckDB: read-only, no file access<br/>Postgres: read-only txn + grants<br/>BigQuery: dry run + byte cap)]
    B --> X[Cell cap + email/phone redaction]
    X --> L[Audit log<br/>no values, no salt]
    L --> A
```

**Pseudonymize at the source, not in the output.** The guard rewrites every dataset reference
into a subquery that replaces personal columns with a salted hash, inside the warehouse. As a
result:
- `count(DISTINCT editor)` stays exact;
- joins on pseudonyms work across datasets;
- a probe like `WHERE editor = 'Alice'` matches nothing, because the comparison runs against
  the hash;
- names never cross the wire.

Masking the output after lineage analysis can be bypassed with aliases, expressions or
functions. Here the raw column is simply not reachable.

**Each layer is designed to hold if the layer before it fails.** The guard is the first layer:
- single statements only;
- SELECT only;
- declared datasets only;
- function allowlist;
- CTE references resolved by scope, so `WITH t AS (SELECT * FROM t)` cannot reach the real
  table `t`;
- capped LIMIT.

Behind it, each engine is locked down on its own, and the tests prove it with the guard
bypassed.

The tools:

| Tool | Use |
|---|---|
| `warehouse_list_datasets` | What the caller's role may query, with each dataset's grain and metrics |
| `warehouse_describe_dataset` | Columns, which are pseudonymized, row policy, and 3 sample rows |
| `warehouse_query_metrics` | **Preferred path.** Named metrics by dimensions and time buckets. The SQL is built as a syntax tree, so filter values are never SQL |
| `warehouse_run_sql` | One guarded SELECT, for questions metrics cannot express |

## What the red team found

The adversarial suite found three real gaps outside the SQL guard. All three are fixed and kept
as regression tests:

1. **ReDoS after the timeout.** The email-redaction regex backtracked quadratically, and a
   1 MB string from `repeat('x', 1000000)` would have held the server for about 12 minutes. It
   ran *after* the warehouse timeout, so nothing stopped it. The fix bounds the regex repeats
   and cuts cells before redaction.
2. **A single huge row skipped the result cap.** The fix halves the longest text cells until
   the row fits.
3. **Pseudonyms could be linked back to names.** An edit to one's own `User:` page pairs a
   pseudonym with a title that contains the name. That re-identified 5.7% of editors in the
   snapshot where it was found. The fix
   adds conditional PII (`pii_when: "namespace IN (2, 3)"`) for user-page titles and
   pseudonymizes edit summaries.

An independent review by a second model then found a gap the suite had missed, this time *in*
the guard:

4. **An alias could disguise a real table as a CTE.** In
   `WITH x AS (...) SELECT * FROM edits_raw x, x`, the guard matched CTE references by alias,
   so the physical table skipped the policy rewrite. CTE references are now resolved by table
   name within each scope, and aliasing a real table to a visible CTE name is refused.
5. **Engine errors quoted data.** A message such as `Could not convert string '<value>'`
   bypassed result redaction and reached both the agent and the audit log. Error messages now
   lose their quoted values and are redacted and capped.

Running the parity cases on BigQuery found one more problem, a correctness bug rather than a
leak: time literals were cast to `DATETIME` against `TIMESTAMP` columns. It is fixed.

## Configure your own warehouse

```yaml
extends: demo:wikipedia            # or write datasets/roles from scratch
connection: {kind: postgres, dsn_env: WAREHOUSE_MCP_POSTGRES_DSN}

roles:
  - {name: analyst, datasets: "*", api_key_env: KEY_ANALYST}
  - name: enwiki_partner             # an external partner sees one wiki only
    datasets: [edits, edits_per_minute]
    row_filters: {edits: "wiki = 'enwiki'", edits_per_minute: "wiki = 'enwiki'"}
    api_key_env: KEY_PARTNER
  - {name: trust_and_safety, datasets: "*", pii_clearance: true, api_key_env: KEY_TS}
```

Datasets declare their columns, which ones are `pii` or `pii_when`, which are `free_text`, and
named metrics. Undeclared columns do not exist for the agent. Over HTTP
(`warehouse-mcp --config my.yaml --transport http`) each bearer key selects a role. Keys come
from environment variables; the config holds no secrets.

Setup for each backend, Claude Desktop snippets and the eval workflow are in
[docs/deployment-guide.md](docs/deployment-guide.md). For rolling this out at a company
(discovery, access model, staged rollout, success metrics, incident runbook), see
[docs/deployment-playbook.md](docs/deployment-playbook.md).

## Development

```bash
pip install -e ".[dev]"
pytest                                    # DuckDB; Postgres/BigQuery suites skip without a DSN
docker compose up -d && python scripts/load_snapshot.py --target postgres
WAREHOUSE_MCP_POSTGRES_DSN=postgresql://mcp_reader:local-only-reader-password@127.0.0.1:55432/warehouse pytest
```

[Architecture and threat model](docs/system-architecture.md) ·
[codebase map](docs/codebase-summary.md) · [roadmap](docs/project-roadmap.md)

## Limits

- Phone-number redaction applies to columns tagged `free_text`. An alias such as `comment AS c`
  loses the phone pass; it keeps email redaction, which applies everywhere.
- HTTP uses static bearer keys and plain HTTP. Put TLS in front of it. OAuth resource-server
  mode is on the roadmap.
- Snowflake is on the roadmap.

## Data

The demo data comes from Wikimedia's public recent-changes stream, licensed CC BY-SA 4.0.
Code is MIT.
