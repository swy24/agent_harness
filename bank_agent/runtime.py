"""Wires the agent with its session store and plugins. The gateway and the evals both use it."""

import uuid
from dataclasses import dataclass, field

from google.adk.apps import App
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types

import config
from bank_agent import request_context
from bank_agent.agent import root_agent
from guardrails.plugins import AuthorizationPlugin, ConfirmationPlugin, FailClosedPlugin, PIIRedactionPlugin
from observability.plugins import CostPlugin, TracingPlugin

APP_NAME = "bank_support"


def default_plugins() -> list[BasePlugin]:
    # Order matters: callbacks run in this order and stop at the first non-None
    # return. Redact first, so that everything after it (traces too) only sees masked data.
    return [PIIRedactionPlugin(), FailClosedPlugin(), TracingPlugin(), AuthorizationPlugin(),
            ConfirmationPlugin(), CostPlugin()]


def recent_conversation(session: Session, max_messages: int = 6) -> str:
    """The last few customer and assistant messages, shared with the sub-agents through state.
    User messages are already PII-redacted when they are stored."""
    lines = []
    for ev in session.events:
        if not ev.content or not ev.content.parts or ev.get_function_calls():
            continue
        text = "".join(p.text or "" for p in ev.content.parts).strip()
        if text and ev.author == "user":
            lines.append(f"Customer: {text}")
        elif text and ev.author == root_agent.name:
            lines.append(f"Assistant: {text}")
    return "\n".join(lines[-max_messages:]) or "(start of conversation)"


@dataclass
class Turn:
    reply: str
    session_id: str
    trace_id: str
    tool_calls: list[str] = field(default_factory=list)  # every tool or sub-agent invoked, in order


class BankAssistant:
    def __init__(self, session_service: BaseSessionService | None = None,
                 extra_plugins: list[BasePlugin] | None = None):
        config.SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
        # Session store: the conversation history and inter-agent shared state survive restarts.
        self.sessions = session_service or SqliteSessionService(str(config.SESSION_DB))
        app = App(name=APP_NAME, root_agent=root_agent, plugins=default_plugins() + (extra_plugins or []))
        self.runner = Runner(app=app, session_service=self.sessions)

    async def ask(self, claims: dict, access_token: str, message: str, session_id: str | None = None) -> Turn:
        user_id = claims["sub"]  # from the verified token, never from the request body
        session = None
        if session_id:  # get_session is scoped by user_id, so another customer's session id cannot be reused
            session = await self.sessions.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        if session is None:
            session = await self.sessions.create_session(app_name=APP_NAME, user_id=user_id)

        trace_id = uuid.uuid4().hex[:16]
        reply, calls = "", []
        reset = request_context.current.set(
            request_context.RequestIdentity(user_id, claims["tier"], access_token, trace_id, message))
        try:
            async for event in self.runner.run_async(
                user_id=user_id,
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=message)]),
                state_delta={"conversation_context": recent_conversation(session)},
            ):
                for fc in event.get_function_calls():
                    calls.append(fc.name)
                if event.is_final_response() and event.content and event.content.parts:
                    reply = "".join(p.text or "" for p in event.content.parts).strip()
        finally:
            request_context.current.reset(reset)
        return Turn(reply=reply or "Sorry, I couldn't process that. Please try again.",
                    session_id=session.id, trace_id=trace_id, tool_calls=calls)
