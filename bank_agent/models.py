"""Model adapter for self-hosted models.

Ollama silently ignores OpenAI's `tool_choice="required"`, so a "you must call a tool"
setting has no effect, and a small model can answer a banking question from memory.
This LiteLlm subclass emulates the setting: when a request demands a tool call
(tool_config mode ANY) and the model replies with text instead, it retries with an
explicit instruction.
"""

from typing import AsyncGenerator

from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

NUDGE = ("You replied without calling a tool. That is not allowed at this step. Respond again, "
         "and this time call the most appropriate tool from the ones available.")


def _forces_tool(req: LlmRequest) -> bool:
    tc = req.config.tool_config if req.config else None
    fcc = tc.function_calling_config if tc else None
    return bool(fcc and fcc.mode and str(fcc.mode).upper().endswith("ANY") and req.tools_dict)


class GroundedLiteLlm(LiteLlm):
    max_retries: int = 2

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if stream or not _forces_tool(llm_request):
            async for r in super().generate_content_async(llm_request, stream):
                yield r
            return

        for attempt in range(self.max_retries + 1):
            responses = [r async for r in super().generate_content_async(llm_request, False)]
            called = any(p.function_call for r in responses if r.content for p in r.content.parts or [])
            if called or attempt == self.max_retries:
                for r in responses:
                    yield r
                return
            llm_request.contents.append(types.Content(role="user", parts=[types.Part(text=NUDGE)]))
