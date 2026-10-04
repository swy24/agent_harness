"""Mock identity provider (IdP).

A real bank would use its existing OIDC provider (Keycloak, Okta, Entra ID,
Ping and so on): the chat UI redirects to it, the customer logs in, and the UI gets
a signed token back. This module plays that role with signed JWTs so the
rest of the system works exactly as it would against a real IdP.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import jwt

import config

_SALT = b"demo-salt"


def _hash(pw: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), _SALT, 100_000).hex()


# username -> identity. customer_id and tier come from the IdP, never from the chat.
USERS = {
    "john": {"customer_id": "C1001", "name": "John", "tier": "privileged", "pw": _hash("john123")},
    "sanjay": {"customer_id": "C1002", "name": "Sanjay", "tier": "standard", "pw": _hash("sanjay123")},
    "priya": {"customer_id": "C1003", "name": "Priya", "tier": "premium", "pw": _hash("priya123")},
}


class AuthError(Exception):
    pass


def login(username: str, password: str) -> str:
    user = USERS.get(username.lower().strip())
    if not user or not hmac.compare_digest(user["pw"], _hash(password)):
        raise AuthError("invalid credentials")
    return issue_token(username.lower().strip())


def issue_token(username: str) -> str:
    u = USERS[username]
    now = datetime.now(timezone.utc)
    claims = {
        "sub": u["customer_id"],
        "name": u["name"],
        "tier": u["tier"],
        "iss": config.JWT_ISSUER,
        "aud": config.JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=config.TOKEN_TTL_MINUTES),
    }
    return jwt.encode(claims, config.JWT_SECRET, algorithm="HS256")


def verify_token(token: str) -> dict:
    """Return the token's claims, or raise AuthError."""
    try:
        return jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=["HS256"],
            audience=config.JWT_AUDIENCE,
            issuer=config.JWT_ISSUER,
            options={"require": ["sub", "tier", "exp"]},
        )
    except jwt.PyJWTError as e:
        raise AuthError(str(e)) from e


def bearer(header_value: str | None) -> dict:
    if not header_value or not header_value.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    return verify_token(header_value[7:])
