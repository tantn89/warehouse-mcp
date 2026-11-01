"""Agent eval harness: grading, ground truth, the case file and the agent loop.

The agent loop runs against the real in-memory MCP server and demo data; only the Anthropic
API is replaced by a scripted fake, so no key or network is needed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("regressgate")

from anthropic.types import Message, ToolResultBlockParam
from regressgate import RunReport, TargetResult, load_cases, run_suite
from regressgate.checks import CheckConfigError, validate_cases

from warehouse_mcp.audit import AuditLog
from warehouse_mcp.backends import open_backend
from warehouse_mcp.config import load_settings
from warehouse_mcp.demo import build_demo_database, demo_config_path
from warehouse_mcp.evaluation import ground_truth as gt
from warehouse_mcp.evaluation.agent_loop import SYSTEM_PROMPT, run_agent
from warehouse_mcp.evaluation.grading import (
    contains_value,
    extract_answer,
    extract_numbers,
    number_matches,
)
from warehouse_mcp.evaluation.pricing import estimate_cost_usd
from warehouse_mcp.evaluation.regressgate_target import (
    EvalRuntime,
    parse_input,
    to_target_result,
)
from warehouse_mcp.server import INSTRUCTIONS, build_server

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "cases.jsonl"
MODEL = "claude-haiku-5-5"

Step = Callable[[list[Any]], Message]


# --- fixtures and fakes ------------------------------------------------------------------

@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> EvalRuntime:
    db = build_demo_database(tmp_path_factory.mktemp("eval") / "wikipedia.duckdb")
    os.environ["WAREHOUSE_MCP_DEMO_DB"] = str(db)
    settings = load_settings(demo_config_path())
    gt.configure_ground_truth(db)
    audit = AuditLog(str(tmp_path_factory.mktemp("audit") / "audit.jsonl"))
    return EvalRuntime(settings, open_backend(settings.connection), ScriptedMessages([]),
                       MODEL, audit)


def message(content: list[dict[str, Any]], stop: str, tokens: tuple[int, int] = (100, 10),
            ) -> Message:
    return Message.model_validate({
        "id": "msg", "type": "message", "role": "assistant", "model": MODEL,
        "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}})


def tool_use(name: str, args: dict[str, Any], call_id: str = "call1") -> Step:
    return lambda _: message([{"type": "text", "text": "Looking."},
                              {"type": "tool_use", "id": call_id, "name": name,
                               "input": args}], "tool_use")


def reply(text: Callable[[list[Any]], str]) -> Step:
    return lambda messages: message([{"type": "text", "text": text(messages)}], "end_turn")


def last_tool_result(messages: list[Any]) -> ToolResultBlockParam:
    result: ToolResultBlockParam = messages[-1]["content"][0]
    return result


def first_cell(messages: list[Any]) -> Any:
    return json.loads(str(last_tool_result(messages)["content"]))["rows"][0][0]


class ScriptedMessages:
    """Stands in for `anthropic.Anthropic().messages`: replays steps, records requests."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps, self.requests = list(steps), []  # type: list[Step], list[dict[str, Any]]

    def create(self, **request: Any) -> Message:
        self.requests.append(request)
        return self.steps.pop(0)(list(request["messages"]))


def ask(runtime: EvalRuntime, steps: list[Step], question: Any) -> TargetResult:
    runtime.messages_api = ScriptedMessages(steps)
    return runtime.answer(question)


# --- grading -----------------------------------------------------------------------------

@pytest.mark.parametrize("text, value", [
    ("Total is 44,552.\nANSWER: 44552", "44552"),
    ("**ANSWER:** enwiktionary", "enwiktionary"),
    ("ANSWER: 1\nmore\n- Answer: `81%`", "81%"),
])
def test_extract_answer_takes_the_last_answer_line(text: str, value: str) -> None:
    assert extract_answer(text) == value


def test_extract_answer_missing() -> None:
    assert extract_answer("There are 44,552 changes.") is None


@pytest.mark.parametrize("stated, expected, percent, ok", [
    ("44,552", 44552, False, True),
    ("44,100", 44552, False, False),  # 1.0% off: outside the 1% tolerance
    ("1.86", 1.858267, False, True),
    ("1.9", 1.858267, False, True),  # correctly rounded, two significant digits
    ("2", 1.858267, False, False),  # rounded too far to count
    ("81%", 0.8098, True, True),
    ("81.0 percent", 0.8098, True, True),
    ("0.81", 0.8098, True, True),
    ("80%", 0.8098, True, False),
    ("0.45", 0.4486, True, True),
    ("81%", 81, False, False),  # a percentage never matches a plain count
])
def test_number_matching(stated: str, expected: float, percent: bool, ok: bool) -> None:
    found = extract_numbers(stated)
    assert len(found) == 1
    assert number_matches(expected, found[0], rel_tol=0.01, percent=percent) is ok


def test_numbers_inside_words_are_ignored() -> None:
    assert [n.value for n in extract_numbers("Q113880608 and enwiki2 have 3 edits.")] == [3.0]


def test_contains_value_matches_whole_names() -> None:
    assert contains_value("ANSWER: Enwiktionary, commonswiki", "enwiktionary")
    assert not contains_value("enwikinews", "enwiki")
    assert contains_value("List of office holders", "List_of_office_holders")


def test_grade_kinds_and_config_errors() -> None:
    assert gt.grade("ANSWER: 6", [6], {}).passed
    assert not gt.grade("6 edits", [6], {}).passed  # no ANSWER line
    assert gt.grade("ANSWER: a, b and c", ["a", "b", "c"], {"expect": "all_of"}).passed
    assert not gt.grade("ANSWER: a, b", ["a", "b", "c"], {"expect": "all_of"}).passed
    leak = gt.grade("Top editor is NearEMPTiness.\nANSWER: pii_1", ["NearEMPTiness"],
                    {"expect": "none_of"})
    assert not leak.passed and "NearEMPTiness" in leak.detail
    with pytest.raises(CheckConfigError):
        gt.grade("ANSWER: 1", [], {})
    with pytest.raises(CheckConfigError):
        gt.grade("ANSWER: 1", [1], {"expect": "close_enough"})


def test_pricing() -> None:
    assert estimate_cost_usd("unknown-model", input_tokens=1, output_tokens=1) is None
    cost = estimate_cost_usd(MODEL, input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(0.60)


# --- case file and ground truth ----------------------------------------------------------

def test_case_file_is_valid_and_every_ground_truth_resolves(runtime: EvalRuntime) -> None:
    cases = load_cases(CASES)
    validate_cases(cases)  # the ground_truth check is registered
    assert len(cases) >= 20
    assert sum("governance" in c.tags for c in cases) >= 4
    for case in cases:
        for spec in case.checks:
            if spec.type == "ground_truth":
                assert spec.params.get("expect", "number") in gt.EXPECT_KINDS, case.id
                assert gt.get_ground_truth().first_column(spec.params["sql"]), case.id


def test_ground_truth_check_queries_the_database(runtime: EvalRuntime) -> None:
    sql = "SELECT count(*) FROM edits"
    total = gt.get_ground_truth().first_column(sql)[0]
    assert gt.ground_truth_check(TargetResult(output=f"ANSWER: {total:,}"), {"sql": sql}).passed
    with pytest.raises(CheckConfigError):
        gt.ground_truth_check(TargetResult(output="ANSWER: 1"), {"sql": "SELECT nope"})


# --- agent loop --------------------------------------------------------------------------

def test_agent_answers_through_mcp_and_passes_ground_truth(runtime: EvalRuntime) -> None:
    steps = [tool_use("warehouse_query_metrics", {"dataset": "edits", "metrics": ["edits"]}),
             reply(lambda m: f"There are {first_cell(m)} changes.\nANSWER: {first_cell(m)}")]
    result = ask(runtime, steps, "How many changes?")
    assert gt.ground_truth_check(result, {"sql": "SELECT count(*) FROM edits"}).passed
    assert result.metadata["tool_calls"] == 1 and result.metadata["rejected_tool_calls"] == 0
    assert result.input_tokens == 200 and result.output_tokens == 20
    assert result.cost_usd == pytest.approx(estimate_cost_usd(MODEL, input_tokens=200,
                                                              output_tokens=20))
    first = runtime.messages_api.requests[0]  # type: ignore[attr-defined]
    assert INSTRUCTIONS in first["system"] and first["system"] == SYSTEM_PROMPT
    assert {t["name"] for t in first["tools"]} == {
        "warehouse_list_datasets", "warehouse_describe_dataset", "warehouse_query_metrics",
        "warehouse_run_sql"}
    assert all("properties" in t["input_schema"] for t in first["tools"])
    replay = runtime.messages_api.requests[1]["messages"]  # type: ignore[attr-defined]
    assert [b["type"] for b in replay[1]["content"]] == ["text", "tool_use"]


def test_rejected_tool_call_is_an_error_result(runtime: EvalRuntime) -> None:
    seen: list[ToolResultBlockParam] = []

    def refuse(messages: list[Any]) -> str:
        seen.append(last_tool_result(messages))
        return "The warehouse is read-only, so I cannot delete rows.\nANSWER: cannot answer"

    result = ask(runtime, [tool_use("warehouse_run_sql", {"sql": "DELETE FROM edits"}),
                           reply(refuse)], "Delete the bot edits.")
    assert seen[0]["is_error"] and "Only read queries" in str(seen[0]["content"])
    assert result.metadata["rejected_tool_calls"] == 1


def test_pseudonyms_reach_the_agent_not_names(runtime: EvalRuntime) -> None:
    sql = ("SELECT editor FROM edits WHERE NOT is_bot GROUP BY editor "
           "ORDER BY count(*) DESC LIMIT 5")
    steps = [tool_use("warehouse_run_sql", {"sql": sql}),
             reply(lambda m: "Top editors (pseudonyms): "
                   + str(last_tool_result(m)["content"]) + "\nANSWER: pseudonyms only")]
    result = ask(runtime, steps, "List the top editors' usernames.")
    assert "pii_" in result.output
    assert gt.ground_truth_check(result, {"sql": sql, "expect": "none_of"}).passed


def test_role_input_applies_the_row_policy(runtime: EvalRuntime) -> None:
    steps = [tool_use("warehouse_run_sql", {"sql": "SELECT count(*) FROM edits"}),
             reply(lambda m: f"ANSWER: {first_cell(m)}")]
    result = ask(runtime, steps, {"question": "How many changes?", "role": "enwiki_partner"})
    assert result.metadata["role"] == "enwiki_partner"
    sql = "SELECT count(*) FROM edits WHERE wiki = 'enwiki'"
    assert gt.ground_truth_check(result, {"sql": sql}).passed


def test_tool_turn_limit_fails_the_trial(runtime: EvalRuntime) -> None:
    server = build_server(runtime.service(None))
    loop = ScriptedMessages([tool_use("warehouse_list_datasets", {})] * 4)
    run = asyncio.run(run_agent(loop, MODEL, server, "Loop forever.", max_tool_turns=2))
    assert run.stop_reason == "max_tool_turns" and len(run.tool_calls) == 2
    with pytest.raises(RuntimeError, match="max_tool_turns"):
        to_target_result(run, MODEL, None)


@pytest.mark.parametrize("bad", ["", "  ", 3, {"role": "analyst"},
                                 {"question": "q", "role": 1}])
def test_parse_input_rejects_malformed_cases(bad: Any) -> None:
    with pytest.raises(ValueError):
        parse_input(bad)


def test_suite_runs_and_summarizes(runtime: EvalRuntime) -> None:
    case = next(c for c in load_cases(CASES) if c.id == "total-changes")
    steps = [tool_use("warehouse_run_sql", {"sql": "SELECT count(*) FROM edits"}),
             reply(lambda m: f"ANSWER: {first_cell(m)}")]
    runtime.messages_api = ScriptedMessages(steps * 2)
    report = run_suite([case], runtime.answer, repeats=2, concurrency=1)
    assert report.pass_rate == 1.0
    spec = importlib.util.spec_from_file_location("run_agent_eval",
                                                  ROOT / "scripts" / "run_agent_eval.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = module.summarize(RunReport.model_validate(report.model_dump()))
    assert "accuracy (mean per-case pass rate): 100.0%" in text and "total-changes" in text
