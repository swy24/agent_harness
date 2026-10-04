"""Service-requests MCP server. Run: uv run python -m mcp_servers.services"""

from mcp.server.mcpserver import Context, MCPServer

from mcp_servers.common import guarded, serve

mcp = MCPServer("services", instructions="Checkbooks, statements, address changes and credit-card services.")


@mcp.tool()
async def request_checkbook(ctx: Context, leaves: int = 25) -> dict:
    """Submit a new checkbook request. It is delivered to the registered address. Only call
    this AFTER the customer has explicitly confirmed that address. To deliver somewhere
    else, the customer must first change the address with update_address."""
    return await guarded(ctx, "request_checkbook", "POST", "/checkbook", {"leaves": leaves})


@mcp.tool()
async def request_statement(ctx: Context, months: int = 3) -> dict:
    """Email an account statement for the last N months (1-12) to the registered email."""
    return await guarded(ctx, "request_statement", "POST", "/statement", {"months": months})


@mcp.tool()
async def update_address(ctx: Context, new_address: str) -> dict:
    """Change the customer's registered postal address."""
    return await guarded(ctx, "update_address", "POST", "/address", {"new_address": new_address})


@mcp.tool()
async def get_credit_card_summary(ctx: Context) -> dict:
    """Get the customer's credit card (masked number), credit limit and outstanding amount."""
    return await guarded(ctx, "get_credit_card_summary", "GET", "/credit-card")


@mcp.tool()
async def start_credit_limit_increase(ctx: Context, new_limit: int) -> dict:
    """Step 1 of a credit limit change: check eligibility and send an OTP to the customer's
    registered mobile. new_limit is in rupees (5 lakhs = 500000)."""
    result = await guarded(ctx, "start_credit_limit_increase", "POST", "/otp")
    return result if result.get("error") else {**result, "new_limit": new_limit,
                                                 "next_step": "ask the customer for the OTP"}


@mcp.tool()
async def confirm_credit_limit_increase(ctx: Context, new_limit: int, otp: str) -> dict:
    """Step 2 of a credit limit change: apply new_limit using the OTP the customer received."""
    return await guarded(ctx, "confirm_credit_limit_increase", "POST", "/credit-card/limit",
                         {"new_limit": new_limit, "otp": otp})


if __name__ == "__main__":
    serve(mcp, "services")
