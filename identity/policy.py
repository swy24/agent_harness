"""Authorization policy: which customer tiers may use which banking capability.

Authentication answers "who is this?". This module answers "what may they do?".
It is enforced twice (defense in depth):
  1. in the agent layer (guardrails/plugins.py), before a tool call is made, so the
     bot can explain the refusal politely;
  2. in the MCP servers, so a bypassed or confused agent still cannot act.
"""

ALL_TIERS = {"standard", "premium", "privileged"}

TOOL_POLICY: dict[str, set[str]] = {
    # accounts
    "get_balance": ALL_TIERS,
    "get_customer_profile": ALL_TIERS,
    # transactions
    "get_recent_transactions": ALL_TIERS,
    "flag_transaction": ALL_TIERS,
    "list_flagged_transactions": ALL_TIERS,
    "check_if_flagged": ALL_TIERS,
    # services
    "request_checkbook": ALL_TIERS,
    "request_statement": ALL_TIERS,
    "update_address": ALL_TIERS,
    "get_credit_card_summary": ALL_TIERS,
    "start_credit_limit_increase": {"privileged"},  # bank policy from the video
    "confirm_credit_limit_increase": {"privileged"},
}


def is_allowed(tool_name: str, tier: str) -> bool:
    """Deny by default: a tool that is missing from the policy is never callable."""
    return tier in TOOL_POLICY.get(tool_name, set())
