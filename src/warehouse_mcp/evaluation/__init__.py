"""Agent evaluation: does Claude answer warehouse questions correctly, and safely, via MCP?

Needs the `eval` extra (`pip install -e ".[eval]"`) and ANTHROPIC_API_KEY for live runs.

How it fits together:

- `agent_loop`: Claude over the Messages API with tool use. The tools are the server's own
  MCP tools (`list_tools` / `call_tool` on an in-memory client), so schemas, server
  instructions, guard rejections and pseudonyms are what a real MCP host would see.
- `evals/cases.jsonl`: regressgate cases. Answer cases store ground-truth SQL; governance
  cases (real usernames, writes, file access) use refusal and leak checks.
- `ground_truth`: the `ground_truth` check. It runs the case SQL directly against the demo
  DuckDB at grading time and grades the agent's `ANSWER:` line deterministically
  (`grading`): numeric tolerance or correct rounding, name and set matching, leak checks.
  No LLM-as-judge.
- `regressgate_target`: the `regressgate run` target (see regressgate.yaml), recording
  tokens, cost (`pricing`), tool calls and rejected tool calls per trial.

Run and gate:

    regressgate run --out head.json
    regressgate compare evals/baseline.json head.json

or `python scripts/run_agent_eval.py --help` for a summary with accuracy, cost per
question, p95 latency and tool usage.
"""
