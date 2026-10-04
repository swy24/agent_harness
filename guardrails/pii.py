"""PII detection and redaction.

Regex plus checksum rules cover the structured identifiers a bank cares about most.
In production you would layer an NER-based detector on top (Microsoft
Presidio, Google Cloud DLP, AWS Comprehend) to catch names and addresses too.
"""

import re

_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_AADHAAR = re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+91[ -]?)?[6-9]\d{9}\b")
_ACCOUNT = re.compile(r"\b\d{9,18}\b")
_CVV = re.compile(r"(?i)\b(cvv|cvc)\W{0,3}\d{3,4}\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group())
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return f"[CARD_ENDING_{digits[-4:]}]"
    return m.group()


# Order matters: the most specific patterns run first.
_RULES: list[tuple[str, re.Pattern, object]] = [
    ("cvv", _CVV, "[CVV_REDACTED]"),
    ("card", _CARD, _card),
    ("aadhaar", _AADHAAR, "[AADHAAR_REDACTED]"),
    ("phone", _PHONE, "[PHONE_REDACTED]"),
    ("account", _ACCOUNT, "[ACCOUNT_NO_REDACTED]"),
    ("pan", _PAN, "[PAN_REDACTED]"),
    ("email", _EMAIL, "[EMAIL_REDACTED]"),
]


def redact(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, kinds_of_pii_found)."""
    found: list[str] = []
    for kind, pattern, repl in _RULES:
        new = pattern.sub(repl, text)
        if new != text:
            found.append(kind)
            text = new
    return text, found


def redact_obj(obj):
    """Recursively redact strings inside tool args and results (dicts, lists)."""
    if isinstance(obj, str):
        return redact(obj)[0]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj
