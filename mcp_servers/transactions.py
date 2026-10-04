"""Transactions MCP server. Run: uv run python -m mcp_servers.transactions"""

import re

from mcp.server.mcpserver import Context, MCPServer

from mcp_servers.common import guarded, serve

mcp = MCPServer("transactions", instructions="Transaction history and suspicious-transaction flags.")


@mcp.tool()
async def get_recent_transactions(ctx: Context, limit: int = 5) -> dict:
    """Get the logged-in customer's most recent transactions, newest first.
    Negative amounts are debits, positive amounts are credits."""
    return {"transactions": await guarded(ctx, "get_recent_transactions", "GET", f"/transactions?limit={int(limit)}")}


@mcp.tool()
async def flag_transaction(ctx: Context, transaction_id: str, reason: str) -> dict:
    """Flag one of the customer's transactions as suspicious, e.g. transaction_id='T501'."""
    if not re.fullmatch(r"T\d{1,8}", transaction_id):  # never splice LLM output into a URL unchecked
        return {"error": True, "status": 400, "detail": "transaction_id must look like 'T501'"}
    return await guarded(ctx, "flag_transaction", "POST", f"/transactions/{transaction_id}/flag", {"reason": reason})


@mcp.tool()
async def check_if_flagged(ctx: Context, transaction_id: str) -> dict:
    """Check whether one specific transaction (e.g. 'T501') was flagged as suspicious.
    Also lists other flagged transactions with the same merchant and amount."""
    flagged = await guarded(ctx, "check_if_flagged", "GET", "/transactions/flagged")
    if isinstance(flagged, dict):  # error
        return flagged
    txns = await guarded(ctx, "check_if_flagged", "GET", "/transactions?limit=20")
    target = next((t for t in txns if t["id"] == transaction_id), None)
    if target is None:
        return {"error": True, "status": 404, "detail": f"{transaction_id} not found"}
    hit = next((f for f in flagged if f["txn_id"] == transaction_id), None)
    similar = [f for f in flagged if f["txn_id"] != transaction_id and f.get("transaction")
               and f["transaction"]["desc"] == target["desc"] and f["transaction"]["amount"] == target["amount"]]
    return {"transaction": target, "flagged": hit is not None, "flag": hit,
            "similar_flagged_transactions": similar}


@mcp.tool()
async def list_flagged_transactions(ctx: Context) -> dict:
    """List transactions the customer has previously flagged as suspicious, with the flag date and reason."""
    return {"flagged": await guarded(ctx, "list_flagged_transactions", "GET", "/transactions/flagged")}


if __name__ == "__main__":
    serve(mcp, "transactions")
