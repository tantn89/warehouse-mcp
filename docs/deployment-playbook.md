# Deployment playbook (forward-deployed rollout)

How to put warehouse-mcp in front of a customer's analysts. Config keys refer to
`src/warehouse_mcp/config.py`; the worked example is `src/warehouse_mcp/demo/wikipedia.yaml`.
Install and run commands live in [deployment-guide.md](deployment-guide.md). Items marked
**not implemented** are process steps the team must do by hand today.

## 1. Discovery checklist

| Question | Why it matters | Where it lands |
|----------|----------------|----------------|
| Which warehouse and engine (DuckDB, Postgres, BigQuery)? Snowflake is not supported yet | Picks `connection.kind` and the safeguards in [system-architecture.md](system-architecture.md) | `connection` |
| Which 5-15 tables answer the real questions? | Start small; every dataset is a liability | `datasets[].table`, `grain`, `time_column` |
| Which columns identify a person, directly or indirectly (names, emails, user ids, free text, titles that embed names)? | Anything missed ships in clear text | `pii`, `pii_when`, `free_text` |
| Who asks what? Interview 3-5 analysts and collect their last 20 real questions | Source for metrics and for eval cases | `metrics`, `evals/cases.jsonl` |
| Who may see which rows (region, tenant, partner)? | Becomes mandatory row filters | `roles[].row_filters` |
| Who needs real identities, and why? | Each cleared role is a standing exception | `roles[].pii_clearance` |
| Cost model: pay-per-scan or fixed capacity? Table sizes? Partitioning on the time column? | Sets the byte cap and teaches agents to filter time | `maximum_bytes_billed`, `limits` |
| Who owns the data, approves access, and reads the audit log? | Named owners, not a team alias | access register (outside the repo) |
| Where does the model run (vendor API, private)? What may leave the network? | Decides how strict PII must be | policy decision |

## 2. Access model design

1. Start from personas, not people: for example `analyst` (all datasets, pseudonymized), `partner`
   (granted datasets plus a row filter), `trust_and_safety` (`pii_clearance: true`). The demo config
   has exactly these three.
2. Grant datasets explicitly (`datasets: [a, b]`); use `"*"` only for roles you would give raw SQL access.
3. Write row filters as plain SQL predicates over the dataset's physical columns, one per dataset
   the role reads. Config validation rejects filters on unknown datasets.
4. Tag PII conservatively. Use `pii_when` when only some rows identify a person (for instance titles
   of user pages). Tag `free_text` on human-written columns. Sweep every column for the same person's
   identity: the red-team suite found names relinked through page titles and edit summaries.
5. Key issuance: one key per role, generated with a secrets library, stored in the customer's secret
   manager and injected as the env var named by `api_key_env`. Never in the YAML (`${ENV}` expansion
   exists for this reason). Keys are per role, not per person, so give each team its own role if
   you need to tell them apart in the audit log.
6. Rotation: replace the env var value and restart. There is no expiry, multi-key overlap or revocation
   list (**not implemented**), so rotate on a calendar and on every offboarding of a key holder.
7. PII salt: set `WAREHOUSE_MCP_PII_SALT` (16-128 letters or digits) from the secret manager so
   pseudonyms stay stable across restarts. Without it each process picks a random salt and
   pseudonyms change on restart.
8. Database identity: connect with a reader that holds `SELECT` on the exposed schema and nothing
   else (`docker/postgres-init.sql` is the reference; BigQuery: `dataViewer` + `jobUser`).

## 3. Semantic model authoring

- Start from the collected questions. For each, find the aggregate and the dimensions it needs;
  those become `metrics` (name, `sql`, description) and listed columns.
- A metric is an aggregate expression over the dataset's own columns, for example `count(DISTINCT editor)`.
  Prefer metrics over letting agents invent SQL: the server compiles them, and the eval measures the gap.
- Descriptions are the agent's documentation. State units, time zone (the server tells agents all times
  are UTC), and what one row means (`grain`).
- Assign every metric a business owner and a review date in the customer's tracker (**not implemented**
  as a config field; `Metric` has only `name`, `sql`, `description`).
- Use `extends` to share a base model across environments (a path, or `demo:wikipedia`); keys you set
  replace the base's keys wholesale. See `examples/postgres.yaml`.
- Expose pre-aggregated marts where possible. They are cheaper and carry less PII than raw events.
- Validate: the server fails at startup when a declared column is missing from the warehouse
  (`validate_against_warehouse`).

## 4. Staged rollout

| Stage | Who | Entry gate | Exit gate |
|-------|-----|-----------|-----------|
| Shadow | Team only, replaying the collected questions | Config validated; eval cases written from the customer's questions | Accuracy and governance cases pass on the customer's data; audit log reviewed line by line |
| Read-only pilot | 3-5 analysts, one or two roles | Shadow exit; keys issued; audit log destination agreed | Two weeks with no governance incident; refusal reasons reviewed; metrics fixed where agents fell back to raw SQL |
| Broader | Remaining analysts and partners | Pilot exit; per-role rate limits are **not implemented**, so size the cost cap per query | Steady cost per question; audit review cadence running |

Set expectations: agents answer from declared datasets only, so "the data isn't there" is a
modeling task, not a bug.

## 5. Success metrics

| Metric | How to measure today | Status |
|--------|----------------------|--------|
| Answer accuracy | `python scripts/run_agent_eval.py` on cases built from the customer's questions (deterministic ground-truth SQL, no LLM judge) | Harness exists; baseline pending <!-- eval results: pending first baseline run --> |
| Governance pass rate | Cases tagged `governance` in the same suite (leaks, writes, file access, row policy) | Same |
| Refusal rate | Count `decision: "denied"` in the audit log against all lines; the eval prints `rejected tool calls` | Manual query; no dashboard |
| Cost per question | Eval reports mean cost per question; BigQuery `bytes_processed` is in the audit log | Eval cost is model spend only; BigQuery cost per query is measured in [bigquery-run.md](bigquery-run.md) |
| Time to answer | Eval p50/p95 latency; `elapsed_ms` in the audit log is query time only | End-to-end latency in production is not measured |
| Audit review | Weekly for the pilot, then monthly: sample denied calls, review any role with `pii_clearance`, check `redacted` counts | Cadence is a process rule, not enforced |

## 6. Incident runbook

The audit log (`audit_log` path, or stderr) is the evidence. Each line has `ts`, `role`, `tool`,
`decision`, `sql`, `sql_sha256`; it never has result values.

| Incident | Steps |
|----------|-------|
| Suspected leak | 1. Revoke: unset or replace the role's env var and restart; stop the process if unsure. 2. Scope: filter the log by `role` and time; group by `sql_sha256` to find repeated or scripted queries. 3. Reconstruct what was returned by re-running the logged `sql` as the same role (the log has no results). 4. If a pseudonym or the salt may be known, rotate `WAREHOUSE_MCP_PII_SALT`: old pseudonyms stop matching new ones, so shared pseudonym lists are invalidated. 5. Review the role's `pii_clearance` and columns tagged `pii`; add missed columns and a regression case. |
| Runaway cost | 1. BigQuery: find large calls by `bytes_processed` in the audit log. 2. Lower `maximum_bytes_billed` (minimum 10 MiB) and restart. 3. Postgres/DuckDB: lower `limits.timeout_s`. 4. Add the offending question as an eval case. Per-role rate limits: **not implemented**. |
| Prompt injection suspected | The role is fixed by the credential, so injected text cannot widen access. Check the log for `denied` bursts from one role, review the SQL the agent attempted, and confirm no write or file function was executed (`decision: "allowed"` with unexpected `datasets`). Remove or tag the poisoned source column; add a `governance` eval case. |
| Key holder leaves | Rotate that role's key; if shared, rotate and redistribute. |

Command sketch, with a placeholder hash and role:

```bash
jq -c 'select(.role=="analyst" and .decision!="allowed")' audit.jsonl | head
jq -r 'select(.sql_sha256=="<hash>") | [.ts,.role,.decision] | @tsv' audit.jsonl
```

## 7. Sample week-1 plan

| Day | Work | Output |
|-----|------|--------|
| Mon | Kickoff; discovery checklist; pick datasets; provision a SELECT-only reader | Signed-off dataset list, named owners |
| Tue | PII sweep with the data owner; draft config with roles and filters | `config.yaml` passing startup validation |
| Wed | Author metrics from the collected questions; run locally with stdio | First questions answered end to end |
| Thu | Write 15-25 eval cases from real questions plus governance cases; first eval run | Baseline report (accuracy, cost, latency) |
| Fri | Fix metrics where the eval failed; review the audit log with the data owner; decide pilot go/no-go | Pilot list of 3-5 analysts, key issuance plan |
