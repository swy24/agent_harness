#!/usr/bin/env bash
# Restart the whole local stack in the background (logs in logs/run_all.log).
cd "$(dirname "$0")"
pkill -f "run_all.py" 2>/dev/null; sleep 1
pkill -f "mcp_servers\.|core_banking.api|gateway.app" 2>/dev/null; sleep 1
nohup uv run python run_all.py "$@" > logs/run_all.log 2>&1 &
URL=http://127.0.0.1:8000/healthz; [[ "$*" == *--no-gateway* ]] && URL=http://127.0.0.1:8100/docs
for _ in $(seq 1 60); do curl -sf "$URL" >/dev/null && break; sleep 0.5; done
echo "stack restarted"
