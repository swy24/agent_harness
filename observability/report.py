"""Troubleshooting and cost CLI.

    uv run python -m observability.report trace <trace_id>   # replay one interaction step by step
    uv run python -m observability.report costs              # spend by customer, agent and model
"""

import json
import sys
from collections import defaultdict

import config


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def trace(trace_id: str) -> None:
    rows = [r for r in _rows(config.TRACE_LOG) if r.get("trace_id") == trace_id]
    if not rows:
        sys.exit(f"no trace {trace_id}")
    for r in rows:
        ev, who = r["event"], r.get("agent") or "-"
        detail = {k: v for k, v in r.items() if k not in ("ts", "trace_id", "user", "agent", "event") and v not in (None, [], "")}
        print(f"{r['ts'][11:]}  {who:<20} {ev:<13} {json.dumps(detail, ensure_ascii=False)[:400]}")


def costs() -> None:
    by = {k: defaultdict(lambda: [0, 0, 0.0]) for k in ("user", "agent", "model")}
    for r in _rows(config.COST_LOG):
        for k in by:
            agg = by[k][r[k]]
            agg[0] += r["input_tokens"]
            agg[1] += r["output_tokens"]
            agg[2] += r["cost_usd"]
    for k, table in by.items():
        print(f"\nby {k}:")
        for name, (tin, tout, usd) in sorted(table.items(), key=lambda kv: -kv[1][2]):
            print(f"  {name:<32} in={tin:>8} out={tout:>7}  ${usd:.4f}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "trace":
        trace(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "costs":
        costs()
    else:
        print(__doc__)
