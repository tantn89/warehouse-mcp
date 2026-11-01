"""Run the agent eval and print what matters: accuracy, cost per question, latency, tools.

    python scripts/run_agent_eval.py --repeats 1 --only total-changes bot-share-overall
    python scripts/run_agent_eval.py --summarize evals/baseline.json

Running needs ANTHROPIC_API_KEY and the eval extra; `--summarize` only reads a report.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

from regressgate import RunReport, load_cases, run_suite


def summarize(report: RunReport) -> str:
    trials = report.trials
    tool_calls = [int(t.metadata.get("tool_calls", 0)) for t in trials if not t.error]
    rejected = sum(int(t.metadata.get("rejected_tool_calls", 0)) for t in trials)
    cost = report.mean_cost_usd()
    lines = [
        f"target: {report.target}  repeats: {report.repeats}  trials: {len(trials)}",
        f"accuracy (mean per-case pass rate): {100 * report.pass_rate:.1f}%",
        f"mean cost / question: {'n/a' if cost is None else f'${cost:.5f}'}",
        f"p50 / p95 latency: {report.latency_quantile(0.5):.1f}s / "
        f"{report.latency_quantile(0.95):.1f}s",
        f"tool calls / question: mean {statistics.fmean(tool_calls or [0]):.1f}, "
        f"max {max(tool_calls or [0])}; rejected tool calls: {rejected}",
        f"errored trials: {sum(t.error is not None for t in trials)}",
        "", "case                              pass  tags",
    ]
    for case in report.cases:
        lines.append(f"{case.id:<34}{case.passes}/{len(case.trials)}   {','.join(case.tags)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--summarize", type=Path, help="Summarize an existing report and exit.")
    parser.add_argument("--cases", type=Path, default=Path("evals/cases.jsonl"))
    parser.add_argument("--only", nargs="+", metavar="CASE_ID", help="Run only these cases.")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--out", type=Path, default=Path(".regressgate/agent-eval.json"))
    args = parser.parse_args(argv)

    if args.summarize:
        print(summarize(RunReport.load(args.summarize)))
        return 0
    # Imported here so --summarize works without the demo database or an API key.
    from warehouse_mcp.evaluation.regressgate_target import answer

    cases = load_cases(args.cases)
    if args.only:
        unknown = set(args.only) - {c.id for c in cases}
        if unknown:
            parser.error(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c.id in set(args.only)]
    report = run_suite(cases, answer, target_name="warehouse_mcp.evaluation.regressgate_target"
                       ":answer", repeats=args.repeats, concurrency=args.concurrency)
    report.save(args.out)
    print(summarize(report))
    print(f"\nReport: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
