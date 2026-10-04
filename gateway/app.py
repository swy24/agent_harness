"""Edge layer and backend API: the only entry point exposed to customers.

What this file does:      In production, put in front of it:
- authentication (JWT)    - WAF (OWASP rules, bot protection) and DDoS protection
- per-customer rate limit - API gateway (Apigee, Kong, AWS API GW) plus the bank's IdP (OIDC)
- input size limits       - TLS termination, private networking to the MCP and core services
- security headers
Run: uv run uvicorn gateway.app:app --port 8000
"""

import logging
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import config
from bank_agent.runtime import BankAssistant
from identity import idp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

app = FastAPI(title="Acme Bank Support Bot", docs_url=None, redoc_url=None)
assistant = BankAssistant()
STATIC = Path(__file__).parent / "static"


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


chat_limiter = SlidingWindowLimiter(config.RATE_LIMIT_REQUESTS, config.RATE_LIMIT_WINDOW_SECONDS)
login_limiter = SlidingWindowLimiter(5, 60)  # slows down credential stuffing


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.update({
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                   "script-src 'self' 'unsafe-inline'",
    })
    return resp


def current_customer(authorization: str | None = Header(None)) -> tuple[dict, str]:
    try:
        claims = idp.bearer(authorization)
    except idp.AuthError:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return claims, authorization[7:]


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


@app.post("/auth/login")
def login(body: LoginIn, request: Request):
    """Stands in for the redirect to the bank's IdP (OIDC authorization-code flow)."""
    if not login_limiter.allow(request.client.host if request.client else "?"):
        raise HTTPException(429, "Too many login attempts")
    try:
        token = idp.login(body.username, body.password)
    except idp.AuthError:
        raise HTTPException(401, "Invalid username or password")
    claims = idp.verify_token(token)
    return {"access_token": token, "name": claims["name"], "tier": claims["tier"]}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=config.MAX_MESSAGE_CHARS)
    session_id: str | None = Field(None, max_length=64)


@app.post("/api/chat")
async def chat(body: ChatIn, who: tuple[dict, str] = Depends(current_customer)):
    claims, token = who
    if not chat_limiter.allow(claims["sub"]):
        raise HTTPException(429, "Too many requests, please slow down")
    try:
        turn = await assistant.ask(claims, token, body.message, body.session_id)
    except Exception:
        log.exception("agent failure customer=%s", claims["sub"])
        raise HTTPException(502, "The assistant is temporarily unavailable")
    # trace_id lets support staff look up exactly what happened (see observability/report.py)
    return {"reply": turn.reply, "session_id": turn.session_id, "trace_id": turn.trace_id}


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
