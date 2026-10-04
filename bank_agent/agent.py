"""The multi-agent system: a coordinator plus domain-specific sub-agents.

    coordinator (plans, delegates, combines the results)
      |- accounts_agent      -> Accounts MCP server
      |- transactions_agent  -> Transactions MCP server
      |- services_agent      -> Services MCP server (+ profile lookup)
      `- insights_agent      -> Transactions MCP server, runs on COMPLEX_MODEL

The specialists are wrapped as AgentTools. The coordinator can therefore call
several of them for one question ("balance AND last transactions") and merge the
answers, which plain agent transfer cannot do.
"""

from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

import config
from bank_agent import prompts, request_context
from bank_agent.models import GroundedLiteLlm

# Self-hosted model for routine work; COMPLEX_MODEL only for heavy reasoning.
local_llm = GroundedLiteLlm(model=config.LOCAL_MODEL)
complex_llm = GroundedLiteLlm(model=config.COMPLEX_MODEL)
# Banking answers should be repeatable, not creative.
precise = types.GenerateContentConfig(temperature=0.1)


def _forward_identity(ctx: ReadonlyContext) -> dict[str, str]:
    """Forward the customer's JWT to the MCP servers, which derive the customer from it."""
    ident = request_context.current.get()
    return {"Authorization": f"Bearer {ident.access_token}"} if ident else {}


def mcp_server(name: str, tool_filter: list[str] | None = None) -> McpToolset:
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"http://{config.HOST}:{config.MCP_PORTS[name]}/mcp", timeout=15
        ),
        header_provider=_forward_identity,
        tool_filter=tool_filter,
    )


def force_tool_on_first_step(callback_context: CallbackContext, llm_request: LlmRequest) -> Optional[LlmResponse]:
    """Grounding guardrail. On the first model call of a turn (the last message is the
    user's), require a tool call: the coordinator must delegate and a specialist must
    fetch data, so neither can answer a banking question from memory. Once tool results
    are in, the model is free to reply."""
    last = llm_request.contents[-1] if llm_request.contents else None
    if last and last.role == "user" and any(p.text for p in last.parts or []):
        llm_request.config.tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="ANY"))
    return None


def general_help(topic: str) -> dict:
    """Use for greetings, thanks, and anything that is not about the customer's Acme Bank
    accounts, transactions, cards or service requests."""
    return {"instruction": "Reply briefly and politely. If this is not a banking request, say you can "
                           "only help with Acme Bank accounts, transactions, cards and service requests, "
                           "and offer examples. Do not fulfil non-banking requests."}


def specialist(name: str, description: str, instruction: str, tools: list, model=local_llm) -> LlmAgent:
    return LlmAgent(
        name=name,
        model=model,
        description=description,
        instruction=instruction + prompts.SHARED_CONTEXT,
        tools=tools,
        generate_content_config=precise,
        before_model_callback=force_tool_on_first_step,
        output_key=f"{name}_result",  # inter-agent shared state, visible to later agents this session
    )


accounts_agent = specialist(
    "accounts_agent",
    "Account balances (savings, fixed deposit) and the customer's registered postal address.",
    prompts.ACCOUNTS, [mcp_server("accounts")])

transactions_agent = specialist(
    "transactions_agent",
    "Recent transactions, flagging a transaction as suspicious, and transactions flagged earlier.",
    prompts.TRANSACTIONS, [mcp_server("transactions")])

services_agent = specialist(
    "services_agent",
    "Credit card details (limit, outstanding amount), credit limit increase with OTP, "
    "checkbook requests, account statements and change of address.",
    prompts.SERVICES, [mcp_server("services"), mcp_server("accounts", tool_filter=["get_customer_profile"])])

insights_agent = specialist(
    "insights_agent",
    "Deeper analysis of spending patterns across many transactions.",
    prompts.INSIGHTS, [mcp_server("transactions", tool_filter=["get_recent_transactions"])],
    model=complex_llm)

root_agent = LlmAgent(
    name="coordinator",
    model=local_llm,
    description="Plans the answer and delegates to the specialist agents.",
    instruction=prompts.COORDINATOR,
    tools=[AgentTool(a) for a in (accounts_agent, transactions_agent, services_agent, insights_agent)]
          + [general_help],
    generate_content_config=precise,
    before_model_callback=force_tool_on_first_step,
)
