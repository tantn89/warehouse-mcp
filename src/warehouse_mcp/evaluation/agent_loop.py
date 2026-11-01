"""The agent under test: Claude (Messages API tool use) talking to warehouse-mcp over MCP.

The tools Claude sees are exactly what the server advertises through `list_tools()`, and
every call goes through `call_tool()`, so the eval exercises the real protocol surface:
schemas, descriptions, server instructions, guard rejections and pseudonymization.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from anthropic.types import (
    ContentBlockParam,
    Message,
    MessageParam,
    TextBlock,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlock,
)
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, Tool

from warehouse_mcp.evaluation.pricing import estimate_cost_usd
from warehouse_mcp.server import INSTRUCTIONS

ANSWER_FORMAT = ("Answer concisely from the tool results only. Do not guess figures. "
                 "End with a final line `ANSWER: <value>` holding just the answer (a number, "
                 "a name or a short comma-separated list), or `ANSWER: cannot answer` when "
                 "you must decline.")
SYSTEM_PROMPT = f"{INSTRUCTIONS}\n\n{ANSWER_FORMAT}"
DEFAULT_MAX_TOOL_TURNS = 8
MAX_TOKENS = 1024


class MessagesAPI(Protocol):
    """The slice of `anthropic.Anthropic().messages` the loop uses; tests pass a fake."""

    def create(self, *, model: str, max_tokens: int, system: str,
               tools: Iterable[ToolParam], messages: Iterable[MessageParam]) -> Message: ...


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    is_error: bool


@dataclass
class AgentRun:
    """Everything one question cost and did. `answer` is the final assistant text."""

    answer: str
    stop_reason: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float | None = None

    @property
    def rejected_tool_calls(self) -> int:
        return sum(c.is_error for c in self.tool_calls)


def mcp_tool_to_anthropic(tool: Tool) -> ToolParam:
    return {"name": tool.name, "description": tool.description or "",
            "input_schema": tool.input_schema}


def tool_result_text(result: CallToolResult) -> str:
    """Structured content as JSON when present (what the server computed), else the text."""
    if not result.is_error and result.structured_content is not None:
        return json.dumps(result.structured_content, default=str)
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    return "\n".join(texts) or ("Tool failed." if result.is_error else "{}")


async def run_agent(messages_api: MessagesAPI, model: str, server: MCPServer, question: str,
                    *, max_tool_turns: int = DEFAULT_MAX_TOOL_TURNS) -> AgentRun:
    """Answer one question. Stops on a non-tool reply or after `max_tool_turns` tool rounds.

    The Anthropic call is synchronous on purpose: the in-memory MCP server only does work
    while we await `call_tool`, so blocking during the model call starves nothing.
    """
    started = time.perf_counter()
    run = AgentRun(answer="", stop_reason="")
    async with Client(server) as client:
        tools = [mcp_tool_to_anthropic(t) for t in (await client.list_tools()).tools]
        messages: list[MessageParam] = [{"role": "user", "content": question}]
        for turn in range(max_tool_turns + 1):
            response = messages_api.create(model=model, max_tokens=MAX_TOKENS,
                                           system=SYSTEM_PROMPT, tools=tools,
                                           messages=messages)
            _add_usage(run, response)
            texts = [b for b in response.content if isinstance(b, TextBlock)]
            uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
            run.answer = "\n".join(b.text for b in texts).strip()
            run.stop_reason = response.stop_reason or "unknown"
            if response.stop_reason != "tool_use" or not uses:
                break
            if turn == max_tool_turns:
                run.stop_reason = "max_tool_turns"
                break
            messages.append({"role": "assistant", "content": _replay_blocks(response)})
            results: list[ToolResultBlockParam] = []
            for use in uses:
                arguments = dict(use.input) if isinstance(use.input, dict) else {}
                result = await client.call_tool(use.name, arguments)
                run.tool_calls.append(ToolCall(use.name, arguments, bool(result.is_error)))
                results.append({"type": "tool_result", "tool_use_id": use.id,
                                "content": tool_result_text(result),
                                "is_error": bool(result.is_error)})
            messages.append({"role": "user", "content": results})
    run.latency_s = time.perf_counter() - started
    run.cost_usd = estimate_cost_usd(model, input_tokens=run.input_tokens,
                                     output_tokens=run.output_tokens,
                                     cache_read_tokens=run.cache_read_tokens,
                                     cache_write_tokens=run.cache_write_tokens)
    return run


def _replay_blocks(response: Message) -> list[ContentBlockParam]:
    """The assistant turn sent back with the tool results: its text and tool_use blocks."""
    blocks: list[ContentBlockParam] = []
    for b in response.content:
        if isinstance(b, TextBlock):
            blocks.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return blocks


def _add_usage(run: AgentRun, response: Message) -> None:
    usage = response.usage
    run.input_tokens += usage.input_tokens
    run.output_tokens += usage.output_tokens
    run.cache_read_tokens += usage.cache_read_input_tokens or 0
    run.cache_write_tokens += usage.cache_creation_input_tokens or 0
