"""Agent evaluation suite: a regression gate for prompt, model and tool changes.

LLM output is non-deterministic, so exact-match unit tests do not work. Instead each
golden case is checked with:
  * trajectory checks: which banking tools were called / succeeded, read from the traces
  * content checks: required / forbidden substrings (numbers normalised)
  * safety checks: raw PII must never appear in anything sent to a model
  * LLM-as-judge: a rubric graded by a model, for behaviour that strings can't capture
Each case runs N times, and we report a pass rate rather than a single pass/fail.

Needs the backends running:  uv run python run_all.py --no-gateway
Run:  uv run python -m evals.run_evals [--runs 3] [--case checkbook] [--no-judge]
"""

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import litellm
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.sessions import InMemorySessionService

import config
from bank_agent.runtime import BankAssistant
from identity import idp

DATASET = Path(__file__).parent / "golden_dataset.json"
PASS_THRESHOLD = 0.66  # a case passes the gate if it passes in at least 2 of 3 runs


class ModelInputRecorder(BasePlugin):
    """Records everything sent to any model (after redaction), keyed by trace id."""

    def __init__(self):
        super().__init__(name="model_input_recorder")
        self.sent: dict[str, list[str]] = {}

    async def before_model_callback(self, *, callback_context, llm_request):
        from bank_agent import request_context
        ident = request_context.current.get()
        self.sent.setdefault(ident.trace_id if ident else "?", []).append(
            llm_request.model_dump_json(include={"contents", "config"}))
        return None


def norm(s: str) -> str:
    s = s.lower().replace("₹", "rs.")
    return re.sub(r"(?<=\d),(?=\d)", "", s)  # 52,340.00 -> 52340.00


def tools_in_trace(trace_id: str) -> tuple[list[str], set[str]]:
    """(tools attempted, tools that returned without an error), read from the observability traces."""
    called, succeeded = [], set()
    for line in config.TRACE_LOG.read_text().splitlines():
        r = json.loads(line)
        if r.get("trace_id") != trace_id:
            continue
        if r["event"] == "tool_call":
            called.append(r["tool"])
        elif r["event"] == "tool_result" and not re.search(r'\\*"error\\*": true', r.get("result", "")):
            succeeded.add(r["tool"])
    return called, succeeded


async def judge(rubric: str, conversation: list[tuple[str, str]]) -> tuple[bool, str]:
    transcript = "\n".join(f"{who.upper()}: {text}" for who, text in conversation)
    prompt = (
        "You are grading a bank customer-support assistant. Ignore style and wording.\n\n"
        f"Conversation:\n{transcript}\n\n"
        f"Question about the LAST assistant reply: {rubric}\n\n"
        "Think briefly, then answer the question with yes or no.\n"
        'Respond with JSON only: {"reasoning": "<short>", "answer": "yes" | "no"}'
    )
    # Ask for yes/no, not a boolean "pass": small judges often flip the polarity of booleans.
    resp = await litellm.acompletion(model=config.JUDGE_MODEL, temperature=0,
                                     messages=[{"role": "user", "content": prompt}],
                                     response_format={"type": "json_object"})
    try:
        verdict = json.loads(resp.choices[0].message.content)
        return str(verdict.get("answer", "")).strip().lower().startswith("y"), verdict.get("reasoning", "")
    except (json.JSONDecodeError, AttributeError):
        return False, "judge returned invalid JSON"


async def run_case(case: dict, use_judge: bool) -> tuple[bool, list[str]]:
    recorder = ModelInputRecorder()
    bot = BankAssistant(session_service=InMemorySessionService(), extra_plugins=[recorder])
    token = idp.issue_token(case["user"])
    claims = idp.verify_token(token)
    session_id, convo, failures = None, [], []

    for i, turn in enumerate(case["turns"], 1):
        t = await bot.ask(claims, token, turn["message"], session_id)
        session_id = t.session_id
        convo += [("customer", turn["message"]), ("assistant", t.reply)]
        exp, reply = turn["expect"], norm(t.reply)
        tools, ok_tools = tools_in_trace(t.trace_id)
        where = f"turn {i}"

        for tool in exp.get("tools_called", []):
            if tool not in tools:
                failures.append(f"{where}: expected tool {tool}, got {tools}")
        if exp.get("tools_called_any") and not set(exp["tools_called_any"]) & set(tools):
            failures.append(f"{where}: expected one of {exp['tools_called_any']}, got {tools}")
        for tool in exp.get("tools_not_called", []):
            if tool in tools:
                failures.append(f"{where}: tool {tool} must not be called")
        for tool in exp.get("tools_succeeded", []):
            if tool not in ok_tools:
                failures.append(f"{where}: expected {tool} to succeed")
        for tool in exp.get("tools_not_succeeded", []):
            if tool in ok_tools:
                failures.append(f"{where}: {tool} must not succeed")
        for s in exp.get("contains", []):
            if norm(s) not in reply:
                failures.append(f"{where}: reply missing {s!r}")
        for s in exp.get("not_contains", []):
            if norm(s) in reply:
                failures.append(f"{where}: reply must not contain {s!r}")
        for s in exp.get("pii_never_sent", []):
            if any(s in payload for payload in recorder.sent.get(t.trace_id, [])):
                failures.append(f"{where}: PII {s!r} was sent to a model!")
        if use_judge and exp.get("rubric"):
            ok, why = await judge(exp["rubric"], convo)
            if not ok:
                failures.append(f"{where}: judge: {why}")
        if failures:
            failures.append(f"{where} reply was: {t.reply[:300]!r}")
            break
    return not failures, failures


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--case", help="only run cases whose id contains this")
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()

    cases = [c for c in json.loads(DATASET.read_text()) if not args.case or args.case in c["id"]]
    results = []
    for case in cases:
        passes, last_fail = 0, []
        t0 = time.time()
        for _ in range(args.runs):
            ok, fails = await run_case(case, not args.no_judge)
            passes += ok
            last_fail = fails or last_fail
        rate = passes / args.runs
        results.append(rate)
        mark = "PASS" if rate >= PASS_THRESHOLD else "FAIL"
        print(f"[{mark}] {case['id']:<45} {passes}/{args.runs}  ({time.time() - t0:.0f}s)")
        if rate < 1:
            for f in last_fail:
                print(f"         - {f}")

    gated = sum(r >= PASS_THRESHOLD for r in results)
    print(f"\n{gated}/{len(results)} cases pass the gate (threshold {PASS_THRESHOLD:.0%}).")
    sys.exit(0 if gated == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
