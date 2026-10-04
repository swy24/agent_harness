"""ADK plugins for the guardrails. Plugins apply globally to every agent, model call and
tool call, including the sub-agents run through AgentTool. That makes them the right
place for policies that must never be skipped."""

import logging
import re
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_tool import McpTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from guardrails.pii import redact, redact_obj
from bank_agent import request_context
from identity import policy

log = logging.getLogger("guardrails")


class PIIRedactionPlugin(BasePlugin):
    """Make sure raw PII never reaches an LLM, self-hosted or third-party, and never
    lands in the session store."""

    def __init__(self) -> None:
        super().__init__(name="pii_redaction")

    async def on_user_message_callback(
        self, *, invocation_context: InvocationContext, user_message: types.Content
    ) -> Optional[types.Content]:
        # Redact at the door, so the conversation history we persist is clean too.
        changed, parts = False, []
        for part in user_message.parts or []:
            if part.text:
                text, kinds = redact(part.text)
                if kinds:
                    changed = True
                    log.warning("PII redacted from user message: %s", kinds)
                parts.append(types.Part(text=text))
            else:
                parts.append(part)
        return types.Content(role=user_message.role, parts=parts) if changed else None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        # Second line of defense: tool results and earlier turns can also carry PII.
        for content in llm_request.contents:
            for part in content.parts or []:
                if part.text:
                    part.text = redact(part.text)[0]
                if part.function_response and part.function_response.response:
                    part.function_response.response = redact_obj(part.function_response.response)
                if part.function_call and part.function_call.args:
                    part.function_call.args = redact_obj(part.function_call.args)
        return None


class AuthorizationPlugin(BasePlugin):
    """Check the tier policy before any banking tool runs. The identity comes from the
    verified JWT claims of this request, never from the conversation."""

    def __init__(self) -> None:
        super().__init__(name="authorization")

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> Optional[dict]:
        if not isinstance(tool, McpTool):
            return None  # sub-agent tools (AgentTool) are not banking actions
        ident = request_context.current.get()
        tier = ident.tier if ident else None
        if not tier or not policy.is_allowed(tool.name, tier):
            log.warning("authz deny tool=%s tier=%s", tool.name, tier)
            return {
                "error": True,
                "status": 403,
                "detail": f"This customer's tier ({tier}) does not permit '{tool.name}'. "
                "Politely tell the customer you cannot do this for them and suggest contacting "
                "their relationship manager. Do not retry.",
            }
        return None


class FailClosedPlugin(BasePlugin):
    """If a specialist's MCP tools failed to load, ADK logs an error and runs the agent
    without tools. A model with no tools but a question about a balance will happily
    invent one. Fail closed instead."""

    def __init__(self, coordinator_name: str = "coordinator") -> None:
        super().__init__(name="fail_closed")
        self.coordinator = coordinator_name

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        if callback_context.agent_name != self.coordinator and not llm_request.tools_dict:
            log.error("agent %s has no tools available; refusing to answer", callback_context.agent_name)
            return LlmResponse(content=types.Content(role="model", parts=[types.Part(
                text="SYSTEM_UNAVAILABLE: banking systems could not be reached. Do not state any "
                     "figures. Apologise and ask the customer to try again shortly.")]))
        return None


class ConfirmationPlugin(BasePlugin):
    """Human in the loop for actions that change something in the real world.

    "Always confirm the delivery address" must not depend only on a prompt that a
    developer can edit (the video's regression). A tool in CONFIRM_TOOLS runs only if
    (1) the assistant's previous message asked the customer to confirm, and
    (2) the customer's current message is an affirmative reply without a negation."""

    CONFIRM_TOOLS = {"request_checkbook", "update_address"}
    AFFIRMATIVE = re.compile(r"\b(yes|yeah|yep|confirm(ed)?|correct|go ahead|proceed|please do|sure|ok(ay)?)\b", re.I)
    NEGATION = re.compile(r"\b(no|not|don'?t|never|without|cancel)\b", re.I)

    def __init__(self) -> None:
        super().__init__(name="confirmation")

    @classmethod
    def confirmed(cls, conversation: str, user_message: str) -> bool:
        last_assistant = next((l for l in reversed(conversation.splitlines()) if l.startswith("Assistant:")), "")
        return ("confirm" in last_assistant.lower()
                and bool(cls.AFFIRMATIVE.search(user_message))
                and not cls.NEGATION.search(user_message))

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> Optional[dict]:
        if tool.name not in self.CONFIRM_TOOLS:
            return None
        ident = request_context.current.get()
        conversation = tool_context.state.get("conversation_context", "") if tool_context else ""
        if ident and self.confirmed(conversation, ident.user_message):
            return None
        log.info("confirmation required before %s", tool.name)
        return {"error": True, "status": "confirmation_required",
                "detail": "Not submitted. The customer has not confirmed yet. Call get_customer_profile, "
                          "show the customer the registered address and ask them to confirm it."}
