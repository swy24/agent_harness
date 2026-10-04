"""Accounts MCP server. Run: uv run python -m mcp_servers.accounts"""

from mcp.server.mcpserver import Context, MCPServer

from mcp_servers.common import guarded, serve

mcp = MCPServer("accounts", instructions="Account balances and customer profile for the logged-in customer.")


@mcp.tool()
async def get_balance(ctx: Context) -> dict:
    """Get the balances of all accounts (savings, fixed deposit) of the logged-in customer."""
    return {"accounts": await guarded(ctx, "get_balance", "GET", "/accounts")}


@mcp.tool()
async def get_customer_profile(ctx: Context) -> dict:
    """Get the logged-in customer's name, tier and registered postal address."""
    return await guarded(ctx, "get_customer_profile", "GET", "/profile")


if __name__ == "__main__":
    serve(mcp, "accounts")
