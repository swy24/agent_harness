"""Central configuration. Every value can be overridden through the environment or a .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# --- LLMs -------------------------------------------------------------------
# Self-hosted model (inside the bank's boundary) used for routine reasoning.
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "ollama_chat/qwen2.5:latest")
# Model used only for "complex reasoning" (spend analysis, disputes). In the
# video this is a third-party API model (e.g. "anthropic/claude-sonnet-5-5" or
# "gemini/gemini-2.5-pro"), or a bigger self-hosted model if your hardware allows.
# It defaults to the local model, so the demo runs fully offline on a 16 GB laptop.
COMPLEX_MODEL = os.getenv("COMPLEX_MODEL", LOCAL_MODEL)
# The LLM-as-judge in the eval suite should ideally be stronger than the model it grades.
JUDGE_MODEL = os.getenv("JUDGE_MODEL", LOCAL_MODEL)
os.environ.setdefault("OLLAMA_API_BASE", "http://localhost:11434")
# ADK probes Google Cloud credentials for MCP mTLS by default. That costs about 9s per MCP
# session off-GCP. Our MCP servers are on the private network, so skip the probe.
os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

# USD per 1M tokens (input, output). Self-hosted models are free per token;
# their cost is infrastructure, so it is tracked separately.
MODEL_PRICES = {
    "ollama_chat/": (0.0, 0.0),
    "anthropic/": (3.0, 15.0),
    "gemini/": (1.25, 10.0),
    "openai/": (2.5, 10.0),
}
DAILY_LLM_BUDGET_USD = float(os.getenv("DAILY_LLM_BUDGET_USD", "0.50"))  # per customer

# --- Identity ---------------------------------------------------------------
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-secret-change-me-in-prod-32b")
JWT_ISSUER = "bank-idp"
JWT_AUDIENCE = "bank-support-bot"
TOKEN_TTL_MINUTES = 30

# --- Services ---------------------------------------------------------------
HOST = "127.0.0.1"
CORE_BANKING_URL = os.getenv("CORE_BANKING_URL", "http://127.0.0.1:8100")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "internal-service-key")  # MCP -> core banking
MCP_PORTS = {"accounts": 8101, "transactions": 8102, "services": 8103}
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8000"))
DEMO_OTP = os.getenv("DEMO_OTP", "246810")  # the "SMS" OTP in this demo

# --- Edge layer -------------------------------------------------------------
RATE_LIMIT_REQUESTS = 4  # per customer ...
RATE_LIMIT_WINDOW_SECONDS = 10  # ... per window (LLM calls are expensive)
MAX_MESSAGE_CHARS = 1000

# --- Storage ----------------------------------------------------------------
SESSION_DB = ROOT / "data" / "sessions.db"
TRACE_LOG = ROOT / "logs" / "traces.jsonl"
COST_LOG = ROOT / "logs" / "costs.jsonl"
