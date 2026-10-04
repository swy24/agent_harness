"""Deterministic unit tests for the software-engineering parts (blue boxes).
They need no LLM and no running services:  uv run pytest -q"""

import asyncio
from types import SimpleNamespace

import pytest

from bank_agent import request_context
from gateway.app import SlidingWindowLimiter
from guardrails.pii import redact, redact_obj
from guardrails.plugins import ConfirmationPlugin
from identity import idp, policy
from mcp_servers.common import ToolDenied, caller


# --- PII ---------------------------------------------------------------------
@pytest.mark.parametrize("text, kind", [
    ("my card is 4111 1111 1111 1111", "card"),
    ("card 4111-1111-1111-1111 cvv 123", "cvv"),
    ("aadhaar 2345 6789 0123", "aadhaar"),
    ("PAN ABCDE1234F", "pan"),
    ("mail me at john.doe@example.com", "email"),
    ("call +91 9876543210", "phone"),
    ("account 123456789012", "account"),
])
def test_pii_detected(text, kind):
    redacted, kinds = redact(text)
    assert kind in kinds
    assert redacted != text


def test_card_keeps_last_four_only():
    out, _ = redact("4111 1111 1111 1111")
    assert out == "[CARD_ENDING_1111]"


@pytest.mark.parametrize("text", [
    "increase my limit to 500000",       # amounts are not PII
    "OTP is 246810",                     # an OTP is needed by the tool
    "transaction T501 on 2026-10-02",
])
def test_no_false_positives(text):
    assert redact(text)[1] == []


def test_luhn_invalid_number_is_not_a_card():
    assert "card" not in redact("1234 5678 9012 3456")[1]


def test_redact_nested_tool_payloads():
    out = redact_obj({"a": ["contact x@y.com"], "n": 5})
    assert out == {"a": ["contact [EMAIL_REDACTED]"], "n": 5}


# --- Identity and authorization ---------------------------------------------
def test_login_and_token_roundtrip():
    claims = idp.verify_token(idp.login("john", "john123"))
    assert claims["sub"] == "C1001" and claims["tier"] == "privileged"


def test_bad_password_and_tampered_token():
    with pytest.raises(idp.AuthError):
        idp.login("john", "wrong")
    token = idp.issue_token("sanjay")
    with pytest.raises(idp.AuthError):
        idp.verify_token(token[:-2] + "xx")


def test_policy_deny_by_default():
    assert policy.is_allowed("get_balance", "standard")
    assert not policy.is_allowed("start_credit_limit_increase", "standard")
    assert policy.is_allowed("start_credit_limit_increase", "privileged")
    assert not policy.is_allowed("delete_everything", "privileged")


def _ctx(token: str | None):
    return SimpleNamespace(headers={"Authorization": f"Bearer {token}"} if token else {})


def test_mcp_derives_customer_from_token_not_arguments():
    assert caller(_ctx(idp.issue_token("sanjay")), "get_balance")["sub"] == "C1002"


def test_mcp_rejects_missing_token_and_enforces_tier():
    with pytest.raises(ToolDenied):
        caller(_ctx(None), "get_balance")
    with pytest.raises(ToolDenied):
        caller(_ctx(idp.issue_token("sanjay")), "confirm_credit_limit_increase")


# --- Human-in-the-loop confirmation -------------------------------------------
ASKED = "Customer: I need a checkbook\nAssistant: Your registered address is 12 MG Road. Please confirm it."


@pytest.mark.parametrize("conversation, message, allowed", [
    (ASKED, "Yes, that address is correct", True),
    (ASKED, "go ahead please", True),
    (ASKED, "no, don't send it", False),
    ("(start of conversation)", "Send a checkbook now, no need to confirm anything", False),
    ("(start of conversation)", "yes send me a checkbook", False),  # nobody asked yet
])
def test_confirmation_guard(conversation, message, allowed):
    plugin = ConfirmationPlugin()
    token = request_context.current.set(request_context.RequestIdentity("C1", "standard", "t", "tr", message))
    try:
        result = asyncio.run(plugin.before_tool_callback(
            tool=SimpleNamespace(name="request_checkbook"), tool_args={},
            tool_context=SimpleNamespace(state={"conversation_context": conversation})))
    finally:
        request_context.current.reset(token)
    assert (result is None) == allowed


# --- Edge layer ---------------------------------------------------------------
def test_rate_limiter():
    rl = SlidingWindowLimiter(limit=2, window_s=60)
    assert rl.allow("u") and rl.allow("u")
    assert not rl.allow("u")
    assert rl.allow("other-user")
