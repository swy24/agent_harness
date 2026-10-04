"""AI observability and cost tracking.

Plain infrastructure monitoring (CPU, memory, HTTP logs) cannot answer "why did the
bot say my balance is 18,200?". For that, every interaction gets a trace id, and we
record which agent ran, what the model was sent and what it decided, which tool it
called with which arguments, what came back, and how long and how much each step cost.

Traces are written as JSON lines to logs/traces.jsonl, so you can grep them or ship
them to any log stack. ADK also emits OpenTelemetry spans natively; point an OTLP
exporter at Jaeger, Langfuse, Phoenix or Cloud Trace for a UI.
"""

import json
import time
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Optional

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

import config
from bank_agent import request_context


def _write(path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _clip(value: Any, n: int = 600) -> Any:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= n else s[:n] + "...<truncated>"


def _trace_id(ctx) -> str:
    # One trace id per customer request, shared by the coordinator and every sub-agent
    # run (each AgentTool call starts a new ADK invocation).
    ident = request_context.current.get()
    return ident.trace_id if ident else ctx.invocation_id


class TracingPlugin(BasePlugin):
    def __init__(self, path=config.TRACE_LOG) -> None:
        super().__init__(name="tracing")
        self.path = path
        self._t0: dict[str, float] = {}

    def _emit(self, ctx, kind: str, **fields) -> None:
        _write(self.path, {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "trace_id": _trace_id(ctx),
            "user": getattr(ctx, "user_id", None),
            "agent": getattr(ctx, "agent_name", None),
            "event": kind,
            **fields,
        })

    async def before_run_callback(self, *, invocation_context: InvocationContext) -> None:
        # By now the PII plugin has already replaced the user message, so only redacted text is logged.
        content = invocation_context.user_content
        if not content:
            return
        text = " ".join(p.text for p in content.parts or [] if p.text)
        _write(self.path, {"ts": datetime.now().isoformat(timespec="milliseconds"),
                           "trace_id": _trace_id(invocation_context),
                           "user": invocation_context.user_id, "event": "user_message", "text": text})

    async def before_agent_callback(self, *, agent: BaseAgent, callback_context: CallbackContext) -> None:
        self._emit(callback_context, "agent_start")

    async def after_agent_callback(self, *, agent: BaseAgent, callback_context: CallbackContext) -> None:
        self._emit(callback_context, "agent_end")

    async def before_model_callback(self, *, callback_context: CallbackContext,
                                    llm_request: LlmRequest) -> None:
        self._t0[f"llm:{callback_context.invocation_id}:{callback_context.agent_name}"] = time.perf_counter()
        last = llm_request.contents[-1] if llm_request.contents else None
        self._emit(callback_context, "llm_request",
                   model=llm_request.model,
                   n_messages=len(llm_request.contents),
                   tools=sorted((llm_request.tools_dict or {}).keys()),
                   last_message=_clip(last.model_dump(exclude_none=True)) if last else None)

    async def after_model_callback(self, *, callback_context: CallbackContext,
                                   llm_response: LlmResponse) -> None:
        t0 = self._t0.pop(f"llm:{callback_context.invocation_id}:{callback_context.agent_name}", None)
        parts = llm_response.content.parts if llm_response.content else []
        usage = llm_response.usage_metadata
        self._emit(callback_context, "llm_response",
                   latency_ms=round((time.perf_counter() - t0) * 1000) if t0 else None,
                   function_calls=[{"name": p.function_call.name, "args": p.function_call.args}
                                   for p in parts if p.function_call],
                   text=_clip(" ".join(p.text for p in parts if p.text)),
                   input_tokens=usage.prompt_token_count if usage else None,
                   output_tokens=usage.candidates_token_count if usage else None,
                   error=llm_response.error_message)

    async def before_tool_callback(self, *, tool: BaseTool, tool_args: dict[str, Any],
                                   tool_context: ToolContext) -> None:
        self._t0[f"tool:{tool_context.function_call_id}"] = time.perf_counter()
        self._emit(tool_context, "tool_call", tool=tool.name, args=tool_args)

    async def after_tool_callback(self, *, tool: BaseTool, tool_args: dict[str, Any],
                                  tool_context: ToolContext, result: dict) -> None:
        t0 = self._t0.pop(f"tool:{tool_context.function_call_id}", None)
        self._emit(tool_context, "tool_result", tool=tool.name,
                   latency_ms=round((time.perf_counter() - t0) * 1000) if t0 else None,
                   result=_clip(result))

    async def on_tool_error_callback(self, *, tool: BaseTool, tool_args: dict[str, Any],
                                     tool_context: ToolContext, error: Exception) -> None:
        self._emit(tool_context, "tool_error", tool=tool.name, args=tool_args, error=repr(error))


def price_per_million(model: str) -> tuple[float, float]:
    for prefix, price in config.MODEL_PRICES.items():
        if model.startswith(prefix):
            return price
    return (5.0, 15.0)  # unknown model: assume expensive, never assume free


class CostPlugin(BasePlugin):
    """Attribute token spend to customer, agent and model, and enforce a daily
    per-customer budget, so one abusive user (or a looping agent) cannot triple the bill."""

    def __init__(self, path=config.COST_LOG, daily_budget_usd: float = config.DAILY_LLM_BUDGET_USD) -> None:
        super().__init__(name="cost")
        self.path = path
        self.budget = daily_budget_usd
        self.spend: dict[tuple[str, str], float] = defaultdict(float)  # (user, day) -> usd
        self._model: dict[str, str] = {}
        self._load_today()

    def _load_today(self) -> None:
        if not self.path.exists():
            return
        today = date.today().isoformat()
        for line in self.path.read_text().splitlines():
            r = json.loads(line)
            if r["ts"].startswith(today):
                self.spend[(r["user"], today)] += r["cost_usd"]

    async def before_model_callback(self, *, callback_context: CallbackContext,
                                    llm_request: LlmRequest) -> Optional[LlmResponse]:
        key = (callback_context.user_id, date.today().isoformat())
        if self.spend[key] >= self.budget:
            return LlmResponse(content=types.Content(role="model", parts=[types.Part(
                text="I've reached my usage limit for today. For anything urgent, please use net "
                     "banking or call customer care.")]))
        self._model[f"{callback_context.invocation_id}:{callback_context.agent_name}"] = llm_request.model or ""
        return None

    async def after_model_callback(self, *, callback_context: CallbackContext,
                                   llm_response: LlmResponse) -> None:
        usage = llm_response.usage_metadata
        if not usage:
            return
        model = self._model.get(f"{callback_context.invocation_id}:{callback_context.agent_name}", "")
        p_in, p_out = price_per_million(model)
        tin, tout = usage.prompt_token_count or 0, usage.candidates_token_count or 0
        cost = (tin * p_in + tout * p_out) / 1_000_000
        self.spend[(callback_context.user_id, date.today().isoformat())] += cost
        _write(self.path, {"ts": datetime.now().isoformat(timespec="seconds"),
                           "trace_id": _trace_id(callback_context), "user": callback_context.user_id,
                           "agent": callback_context.agent_name, "model": model,
                           "input_tokens": tin, "output_tokens": tout, "cost_usd": round(cost, 6)})
