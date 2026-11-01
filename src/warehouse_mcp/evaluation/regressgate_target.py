"""Entry point for `regressgate run` (see regressgate.yaml): the agent as a regressgate target.

A case input is a question string, or `{"question": ..., "role": ...}` to ask as another
role of the demo config (row policies, dataset grants). Settings come from the environment:

    ANTHROPIC_API_KEY          required for live runs
    WAREHOUSE_MCP_EVAL_MODEL   default claude-haiku-5-5
    WAREHOUSE_MCP_EVAL_AUDIT   audit log path, default .regressgate/eval-audit.jsonl

Importing this module also registers the `ground_truth` check, which regressgate needs
before it validates the cases.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from regressgate import TargetResult

from warehouse_mcp.audit import AuditLog
from warehouse_mcp.backends import Backend, open_backend
from warehouse_mcp.config import Settings, load_settings
from warehouse_mcp.evaluation import ground_truth  # noqa: F401  (registers the check)
from warehouse_mcp.evaluation.agent_loop import AgentRun, MessagesAPI, run_agent
from warehouse_mcp.server import build_server
from warehouse_mcp.service import WarehouseService

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_AUDIT_LOG = ".regressgate/eval-audit.jsonl"


def parse_input(case_input: Any) -> tuple[str, str | None]:
    """(question, role or None for the config default), validated at the boundary."""
    if isinstance(case_input, str) and case_input.strip():
        return case_input, None
    if isinstance(case_input, dict):
        question, role = case_input.get("question"), case_input.get("role")
        if isinstance(question, str) and question.strip() and (role is None
                                                               or isinstance(role, str)):
            return question, role
    raise ValueError("case input must be a question or {'question': str, 'role': str}")


class EvalRuntime:
    """One backend and Anthropic client for the process; one service per role, built lazily.

    regressgate calls the target from several threads, so building is guarded by a lock.
    """

    def __init__(self, settings: Settings, backend: Backend, messages_api: MessagesAPI,
                 model: str, audit: AuditLog) -> None:
        self._settings, self._backend, self._audit = settings, backend, audit
        self.messages_api, self.model = messages_api, model
        self._services: dict[str, WarehouseService] = {}
        self._lock = threading.Lock()

    def service(self, role: str | None) -> WarehouseService:
        name = self._settings.role(role or self._settings.default_role).name
        with self._lock:
            if name not in self._services:
                # stdio-style serving: with no bearer token the server uses default_role.
                settings = self._settings.model_copy(update={"default_role": name})
                service = WarehouseService(settings, self._backend, self._audit)
                service.validate_against_warehouse()
                self._services[name] = service
            return self._services[name]

    def answer(self, case_input: Any) -> TargetResult:
        question, role = parse_input(case_input)
        server = build_server(self.service(role))
        run = asyncio.run(run_agent(self.messages_api, self.model, server, question))
        return to_target_result(run, self.model, role)


def to_target_result(run: AgentRun, model: str, role: str | None) -> TargetResult:
    if run.stop_reason == "max_tool_turns" or not run.answer:
        # regressgate records this as a failed trial with the error, not as a wrong answer.
        raise RuntimeError(f"agent stopped without an answer ({run.stop_reason})")
    return TargetResult(
        output=run.answer, input_tokens=run.input_tokens + run.cache_read_tokens
        + run.cache_write_tokens, output_tokens=run.output_tokens, cost_usd=run.cost_usd,
        metadata={"model": model, "role": role, "stop_reason": run.stop_reason,
                  "tool_calls": len(run.tool_calls),
                  "tools": [c.name for c in run.tool_calls],
                  "rejected_tool_calls": run.rejected_tool_calls,
                  "agent_latency_s": round(run.latency_s, 3)})


def build_runtime() -> EvalRuntime:
    """The live runtime: demo config, Anthropic client from ANTHROPIC_API_KEY."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("set ANTHROPIC_API_KEY to run the agent eval")
    import anthropic

    from warehouse_mcp.demo import demo_config_path

    settings = load_settings(demo_config_path())
    audit = AuditLog(os.environ.get("WAREHOUSE_MCP_EVAL_AUDIT", DEFAULT_AUDIT_LOG))
    client = anthropic.Anthropic(max_retries=4)
    return EvalRuntime(settings, open_backend(settings.connection), client.messages,
                       os.environ.get("WAREHOUSE_MCP_EVAL_MODEL", DEFAULT_MODEL), audit)


_runtime: EvalRuntime | None = None
_runtime_lock = threading.Lock()


def answer(case_input: Any) -> TargetResult:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = build_runtime()
    return _runtime.answer(case_input)
