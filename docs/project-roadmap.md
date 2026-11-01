# Roadmap

Direction, not a commitment. Items are unstarted unless marked. Shipped behavior is described by the
code and by [project-overview-pdr.md](project-overview-pdr.md).

## v0.1 (current: `0.1.0`)

Done:

- Four read-only MCP tools over DuckDB, Postgres and BigQuery.
- SQL guard with policy-subquery rewrite: declared columns, row filters, salted pseudonyms, `pii_when`.
- Semantic layer: curated metrics compiled to SQL; `extends` for shared models.
- Backend safeguards: read-only connections, timeouts, BigQuery dry run and byte cap.
- Free-text redaction (emails, phones), cell and result caps, JSONL audit log.
- Stdio and streamable HTTP with per-role bearer keys.
- Red-team suite (three gaps found and fixed), DuckDB and Postgres parity tests, CI.
- Agent eval harness with deterministic grading and a regression gate.

Open at v0.1 (measure before claiming):

- First baseline eval run: accuracy, cost per question, latency. <!-- eval results: pending first baseline run -->
- First live BigQuery run: bytes billed against the cap. <!-- BigQuery run results: pending first live run -->

## Next

| Item | Why | Notes |
|------|-----|-------|
| Snowflake backend | Common customer warehouse; today only DuckDB, Postgres, BigQuery | Needs a `Backend` implementation, a connection model, a read-only role recipe and parity/red-team coverage |
| OAuth resource-server mode | Static bearer keys cannot expire or identify individuals | The server would validate tokens from a customer's identity provider; it would not become an authorization server |
| Column lineage for free-text redaction beyond names | Tagged columns and regexes miss names typed into free text | Would extend masking beyond declared PII columns |
| Per-role rate limits | Bound spend and abuse per credential | Not implemented; cost today is capped per query only |
| MCP Registry listing | Discoverability | Requires a published release |
| More eval cases | Cover joins, time ranges, governance and more roles; customer-specific suites | Cases are JSONL in `evals/cases.jsonl` |

## Explicitly out of scope

Writes, a UI, reversible tokenization. See non-goals in [project-overview-pdr.md](project-overview-pdr.md).
