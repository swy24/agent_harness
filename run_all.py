"""Start the whole stack locally: core banking, 3 MCP servers and the gateway.

    uv run python run_all.py         # then open http://127.0.0.1:8000
    uv run python run_all.py --no-gateway   # backends only (for the evals)
"""

import signal
import subprocess
import sys
import time
import urllib.request

import config

SERVICES = [
    ("core-banking", ["-m", "uvicorn", "core_banking.api:app", "--port", "8100", "--log-level", "warning"]),
    ("mcp-accounts", ["-m", "mcp_servers.accounts"]),
    ("mcp-transactions", ["-m", "mcp_servers.transactions"]),
    ("mcp-services", ["-m", "mcp_servers.services"]),
    ("gateway", ["-m", "uvicorn", "gateway.app:app", "--port", str(config.GATEWAY_PORT)]),
]


def check_ollama() -> None:
    base = __import__("os").environ["OLLAMA_API_BASE"]
    try:
        urllib.request.urlopen(f"{base}/api/tags", timeout=3)
    except Exception:
        sys.exit(f"Ollama is not reachable at {base}. Start it with `ollama serve`.")


def main() -> None:
    check_ollama()
    services = [s for s in SERVICES if not ("--no-gateway" in sys.argv and s[0] == "gateway")]
    procs = []
    for name, args in services:
        procs.append((name, subprocess.Popen([sys.executable, *args], cwd=config.ROOT)))
        print(f"started {name}")
        time.sleep(0.8)
    print(f"\nModels: local={config.LOCAL_MODEL}  complex={config.COMPLEX_MODEL}")
    if "--no-gateway" not in sys.argv:
        print(f"Open http://{config.HOST}:{config.GATEWAY_PORT}  (users: john/john123, sanjay/sanjay123, priya/priya123)")
    print("Ctrl+C to stop.\n")

    def stop(*_):
        for _, p in procs:
            p.terminate()
        for _, p in procs:
            p.wait(timeout=10)
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while True:
        for name, p in procs:
            if p.poll() is not None:
                print(f"{name} exited with code {p.returncode}; shutting down")
                stop()
        time.sleep(1)


if __name__ == "__main__":
    main()
