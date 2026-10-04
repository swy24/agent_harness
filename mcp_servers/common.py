"""Shared plumbing for the MCP servers.

The MCP servers own all of the API engineering: endpoints, schemas, errors and auth.
The agents only reason about *which* tool to use.

Key security property: no tool takes a `customer_id` argument. The customer
is derived from the JWT that the agent forwards in the Authorization header, so
the LLM (or a user who prompt-injects it) cannot ask for another customer's data.
"""

import httpx
from mcp.server.mcpserver import Context

import config
from identity import idp, policy


class ToolDenied(Exception):
    pass


def caller(ctx: Context, tool_name: str) -> dict:
    """Authenticate (JWT) and authorize (tier policy) the caller of a tool."""
    headers = {k.lower(): v for k, v in (ctx.headers or {}).items()}
    try:
        claims = idp.bearer(headers.get("authorization"))
    except idp.AuthError as e:
        raise ToolDenied(f"unauthenticated: {e}") from e
    if not policy.is_allowed(tool_name, claims["tier"]):
        raise ToolDenied(f"customer tier '{claims['tier']}' is not permitted to use {tool_name}")
    return claims


async def core(method: str, path: str, customer_id: str, json: dict | None = None):
    """Call the core-banking API. Errors come back as data the agent can explain."""
    async with httpx.AsyncClient(base_url=config.CORE_BANKING_URL, timeout=10) as client:
        resp = await client.request(
            method,
            f"/customers/{customer_id}{path}",
            json=json,
            headers={"x-internal-key": config.INTERNAL_API_KEY},
        )
    if resp.status_code >= 400:
        detail = resp.json().get("detail", resp.text) if resp.content else resp.reason_phrase
        return {"error": True, "status": resp.status_code, "detail": detail}
    return resp.json()


async def guarded(ctx: Context, tool_name: str, method: str, path: str, json: dict | None = None):
    try:
        claims = caller(ctx, tool_name)
    except ToolDenied as e:
        return {"error": True, "status": 403, "detail": str(e)}
    return await core(method, path, claims["sub"], json)


def serve(server, name: str) -> None:
    server.run(
        "streamable-http",
        host=config.HOST,
        port=config.MCP_PORTS[name],
        stateless_http=True,
    )
