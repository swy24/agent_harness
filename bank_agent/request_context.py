"""Per-request identity, carried in a ContextVar.

Why not session state? Anything in non-temp state is persisted to the session store
(and an access token must never be). `temp:` state is also not copied into the
child sessions that AgentTool creates for sub-agents. A ContextVar follows the
async call chain into every sub-agent, tool and plugin of this request, and
disappears when the request ends.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestIdentity:
    customer_id: str
    tier: str
    access_token: str
    trace_id: str
    user_message: str = ""  # this turn's message, used by the confirmation guard


current: ContextVar[RequestIdentity | None] = ContextVar("request_identity", default=None)
