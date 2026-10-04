"""Mock core-banking system: the internal APIs that net banking already uses.

The bank owns this. The AI system never talks to it directly; only the MCP
servers do, from inside the network, using a service key.
Run: uv run uvicorn core_banking.api:app --port 8100
"""

from datetime import date, datetime, timedelta
from itertools import count

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import config

app = FastAPI(title="Core Banking (mock)")
_today = date.today()
_ids = count(9000)


def _d(days_ago: int) -> str:
    return (_today - timedelta(days=days_ago)).isoformat()


CUSTOMERS = {
    "C1001": {
        "name": "John Mathew",
        "tier": "privileged",
        "address": "12 MG Road, Bengaluru 560001",
        "accounts": [
            {"account": "XXXXXX4521", "type": "savings", "balance": 52340.00, "currency": "INR"},
            {"account": "XXXXXX7788", "type": "fixed_deposit", "balance": 300000.00, "currency": "INR"},
        ],
        "credit_card": {"card": "XXXX-XXXX-XXXX-9012", "limit": 300000, "outstanding": 18200.0},
        "transactions": [
            {"id": "T501", "date": _d(1), "desc": "AMAZON PAY INDIA", "amount": -1200.0},
            {"id": "T500", "date": _d(3), "desc": "SALARY ACME CORP", "amount": 95000.0},
            {"id": "T499", "date": _d(4), "desc": "SWIGGY", "amount": -640.0},
            {"id": "T498", "date": _d(6), "desc": "ELECTRICITY BESCOM", "amount": -2310.0},
            {"id": "T497", "date": _d(8), "desc": "AMAZON PAY INDIA", "amount": -1200.0},
            {"id": "T496", "date": _d(12), "desc": "ATM WITHDRAWAL", "amount": -5000.0},
        ],
        # Flagged through this same chatbot "last week" -> long-term memory lives
        # in the system of record, not in the LLM.
        "flagged": [{"txn_id": "T497", "reason": "Did not recognise this Amazon charge", "flagged_on": _d(7)}],
    },
    "C1002": {
        "name": "Sanjay Kumar",
        "tier": "standard",
        "address": "45 Anna Salai, Chennai 600002",
        "accounts": [{"account": "XXXXXX1133", "type": "savings", "balance": 24345.00, "currency": "INR"}],
        "credit_card": {"card": "XXXX-XXXX-XXXX-3344", "limit": 100000, "outstanding": 4100.0},
        "transactions": [
            {"id": "T601", "date": _d(2), "desc": "FLIPKART", "amount": -3499.0},
            {"id": "T600", "date": _d(5), "desc": "SALARY GLOBEX", "amount": 48000.0},
            {"id": "T599", "date": _d(9), "desc": "UBER", "amount": -320.0},
        ],
        "flagged": [],
    },
    "C1003": {
        "name": "Priya Sharma",
        "tier": "premium",
        "address": "7 Park Street, Kolkata 700016",
        "accounts": [{"account": "XXXXXX5560", "type": "savings", "balance": 131020.50, "currency": "INR"}],
        "credit_card": {"card": "XXXX-XXXX-XXXX-7781", "limit": 200000, "outstanding": 0.0},
        "transactions": [{"id": "T701", "date": _d(1), "desc": "ZOMATO", "amount": -455.0}],
        "flagged": [],
    },
}
SERVICE_REQUESTS: list[dict] = []


def internal_only(x_internal_key: str = Header(...)):
    if x_internal_key != config.INTERNAL_API_KEY:
        raise HTTPException(403, "internal callers only")


def customer(cid: str) -> dict:
    if cid not in CUSTOMERS:
        raise HTTPException(404, "customer not found")
    return CUSTOMERS[cid]


def _service_request(cid: str, kind: str, **details) -> dict:
    req = {"request_id": f"SR{next(_ids)}", "customer_id": cid, "type": kind,
           "status": "submitted", "created_at": datetime.now().isoformat(timespec="seconds"), **details}
    SERVICE_REQUESTS.append(req)
    return {k: v for k, v in req.items() if k != "customer_id"}


api = Depends(internal_only)


@app.get("/customers/{cid}/accounts", dependencies=[api])
def accounts(cid: str):
    return customer(cid)["accounts"]


@app.get("/customers/{cid}/profile", dependencies=[api])
def profile(cid: str):
    c = customer(cid)
    return {"name": c["name"], "tier": c["tier"], "registered_address": c["address"]}


@app.get("/customers/{cid}/transactions", dependencies=[api])
def transactions(cid: str, limit: int = 5):
    return customer(cid)["transactions"][: max(1, min(limit, 20))]


class Flag(BaseModel):
    reason: str = Field(max_length=200)


@app.post("/customers/{cid}/transactions/{tid}/flag", dependencies=[api])
def flag(cid: str, tid: str, body: Flag):
    c = customer(cid)
    if not any(t["id"] == tid for t in c["transactions"]):
        raise HTTPException(404, "transaction not found for this customer")
    entry = {"txn_id": tid, "reason": body.reason, "flagged_on": _today.isoformat()}
    c["flagged"].append(entry)
    return entry


@app.get("/customers/{cid}/transactions/flagged", dependencies=[api])
def flagged(cid: str):
    c = customer(cid)
    by_id = {t["id"]: t for t in c["transactions"]}
    return [{**f, "transaction": by_id.get(f["txn_id"])} for f in c["flagged"]]


class Checkbook(BaseModel):
    leaves: int = Field(25, ge=10, le=100)


@app.post("/customers/{cid}/checkbook", dependencies=[api])
def checkbook(cid: str, body: Checkbook):
    # Always the registered address: a delivery address is never taken from free text.
    return _service_request(cid, "checkbook", leaves=body.leaves, delivery_address=customer(cid)["address"])


class Address(BaseModel):
    new_address: str = Field(min_length=10, max_length=200)


@app.post("/customers/{cid}/address", dependencies=[api])
def update_address(cid: str, body: Address):
    customer(cid)["address"] = body.new_address
    return {"status": "updated", "registered_address": body.new_address}


class Statement(BaseModel):
    months: int = Field(3, ge=1, le=12)


@app.post("/customers/{cid}/statement", dependencies=[api])
def statement(cid: str, body: Statement):
    customer(cid)
    return _service_request(cid, "statement", months=body.months, delivery="registered email")


@app.get("/customers/{cid}/credit-card", dependencies=[api])
def credit_card(cid: str):
    return customer(cid)["credit_card"]


@app.post("/customers/{cid}/otp", dependencies=[api])
def send_otp(cid: str):
    customer(cid)
    print(f"[SMS gateway] OTP for {cid}: {config.DEMO_OTP}", flush=True)
    return {"status": "otp_sent", "channel": "registered mobile"}


class LimitChange(BaseModel):
    new_limit: int = Field(gt=0, le=2_000_000)
    otp: str


@app.post("/customers/{cid}/credit-card/limit", dependencies=[api])
def change_limit(cid: str, body: LimitChange):
    c = customer(cid)
    if body.otp != config.DEMO_OTP:
        raise HTTPException(401, "invalid OTP")
    c["credit_card"]["limit"] = body.new_limit
    return {"status": "limit_updated", "new_limit": body.new_limit}
