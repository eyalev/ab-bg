#!/usr/bin/env python3
"""Run browser-use/jev-ultrafast's loop with ab-bg pick's destructive-intent gate in front of every step.

jev-ultrafast (github.com/browser-use/jev-ultrafast) is a complete browser agent: a DOM
element table per observation, TypeSafe's Jev choosing an operation and a target in one
request, the app executing from its own node reference. What it does not have is a
question about whether the GOAL is something a person would want to confirm first. ab-bg
pick-v2 has exactly that — a separate Noul about the intent as a whole, gated low — so this
puts the two together without modifying either:

    predict  →  [gate: goal Noul once, step Noul every action]  →  act

The gate asks Jev two more yes/no questions, in one extra call per step, and stops the run
(exit 5, JSON says what it would have done) when either is at or above --danger (0.3, the
same asymmetric bar as pick: a wrong stop costs a re-run, a wrong click cannot be undone).
--force records that a person decided and lets the run proceed. The agent itself is untouched
and still applies its own guards (fail-closed ids, freshness, occlusion).

Usage (from the jev-ultrafast checkout, so its venv is used):
    cd ~/projects/github/browser-use/jev-ultrafast
    BU_CDP_URL=http://127.0.0.1:9222 TYPESAFE_API_KEY=$(cat ~/.config/desk/typesafe.key) \
      uv run python ~/projects/personal/2026-09/ab-bg/examples/jev-ultrafast-gated.py \
      --url 'https://accounts.google.com/AccountChooser?continue=https://myaccount.google.com/' \
      --goal 'Choose the eyalev@gmail.com account to continue.' [--danger 0.3] [--force] [--max-steps 12]

BU_CDP_URL points Browser Harness at the shared signed-in debug Chrome; the agent opens its
own background tab there and closes it at the end. Every step is appended to
~/.cache/ab-bg/ultrafast.jsonl. Journal: ~/.claude/docs/typesafe-jev.md.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from jev_ultrafast import Agent

GATE_VERSION = "ultrafast-gate-v1"
LOG = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ab-bg" / "ultrafast.jsonl"

GOAL_Q = ("Carrying out the GOAL as a whole would delete, remove, revoke, cancel, pay, purchase, "
          "transfer money, send a message or email, publish or post publicly, change account security "
          "(password, keys, 2FA, linked accounts), or otherwise do something hard to undo. Navigating, "
          "reading, opening a menu or dialog, searching, filtering, logging in, choosing which signed-in "
          "account to continue with, or filling a field without submitting is NOT this.")
STEP_Q = ("The NEXT ACTION, by itself, performs something hard to undo: it submits a payment, deletes or "
          "removes something, sends or publishes, confirms an irreversible dialog, or changes account "
          "security. Clicking into a field, opening a menu, choosing an account, navigating, scrolling or "
          "waiting is NOT this.")


def ask_gate(key, goal, page, action_label, history):
    state = {
        "goal": goal,
        "page_url": page.get("url", ""),
        "next_action": action_label,
        "recent_actions": [h["action"] for h in history[-5:]],
        "visible_elements": [a.get("label", "") for a in page.get("actions", [])][:60],
    }
    body = {"state": state, "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
            "questions": {"goal_destructive": {"type": "noul", "instructions": GOAL_Q},
                          "step_destructive": {"type": "noul", "instructions": STEP_Q}}}
    t0 = time.perf_counter()
    r = httpx.post("https://api.typesafe.ai/v1/systemone", json=body, timeout=30,
                   headers={"authorization": f"Bearer {key}"})
    r.raise_for_status()
    a = r.json()["answers"]
    return float(a["goal_destructive"]["noul"]), float(a["step_destructive"]["noul"]), round((time.perf_counter() - t0) * 1000)


def log(row):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "gate": GATE_VERSION, **row}) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--danger", type=float, default=0.3)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-steps", type=int, default=12)
    args = ap.parse_args()
    key = os.environ.get("TYPESAFE_API_KEY") or sys.exit("TYPESAFE_API_KEY missing; nothing run")

    started = time.perf_counter()
    with Agent(args.url, args.goal) as agent:
        st = agent.state

        retries = 0

        def reobserve():
            # After a click that navigates, the document is "navigating" for a few hundred
            # ms and observe() refuses; wait it out rather than treat it as a failure.
            st["decision"] = None; st["status"] = "ready"
            for _ in range(30):
                try:
                    st["page"] = st["browser"].observe(screenshot=False); return
                except Exception:
                    time.sleep(0.1)
            st["page"] = st["browser"].observe(screenshot=False)

        def give_up(reason):
            row = {"final": "blocked", "reason": reason, "steps": len(st["history"]),
                   "total_ms": round((time.perf_counter() - started) * 1000), "url": st["page"].get("url")}
            log(row); print(json.dumps(row)); sys.exit(3)

        # The goal is judged ONCE, up front, on the first page, before the agent predicts
        # anything -- a destructive goal is stopped before a single element is chosen, and
        # the number is on record even when the agent then blocks itself.
        goal_p, _, gate_ms = ask_gate(key, args.goal, st["page"], "(none yet)", [])
        row = {"step": 0, "goal_destructive": round(goal_p, 3), "gate_ms": gate_ms, "url": st["page"].get("url")}
        if goal_p >= args.danger and not args.force:
            row.update(stopped=f"destructive goal {goal_p:.2f} >= {args.danger}; --force if a person has decided", acted=False)
            log(row); print(json.dumps(row)); sys.exit(5)
        if args.force and goal_p >= args.danger:
            row["forced"] = True
        log(row); print(json.dumps(row))

        for step in range(1, args.max_steps + 1):
            agent.command("predict")
            d = st["decision"]
            choice = d["choice"]
            page = st["page"]
            if choice in {"DONE", "BLOCKED"}:
                try:
                    out = agent.command("act", {"fingerprint": page["fingerprint"]})
                except Exception as e:  # StalePage: the page moved under the verdict; look again
                    log({"step": step, "choice": choice, "retry": str(e)[:120]}); reobserve(); continue
                row = {"step": step, "choice": choice, "confidence": round(d["confidence"], 3), "status": out["status"],
                       "elapsed_ms": out["elapsed_ms"], "url": page.get("url")}
                log(row); print(json.dumps(row))
                break
            action = next(a for a in page["actions"] if a["id"] == choice)
            label = action.get("label", choice)
            _, sp, gate_ms = ask_gate(key, args.goal, page, label, st["history"])
            row = {"step": step, "operation": d.get("operation"), "target": d.get("target"), "action": label,
                   "p": round(d["probabilities"].get(choice, 0), 3), "confidence": round(d["confidence"], 3),
                   "step_destructive": round(sp, 3), "gate_ms": gate_ms,
                   "decide_ms": d.get("latency_ms"), "url": page.get("url")}
            if sp >= args.danger and not args.force:
                row.update(stopped=f"destructive step {sp:.2f} >= {args.danger}; --force if a person has decided",
                           would_have=f"{d.get('operation')} {label}", acted=False)
                log(row); print(json.dumps(row))
                sys.exit(5)
            if args.force and sp >= args.danger:
                row["forced"] = True
            try:
                out = agent.command("act", {"fingerprint": page["fingerprint"]})
            except Exception as e:  # StalePage or a guard inside the agent: observe again, do not count it
                row.update(acted=False, retry=str(e)[:120]); log(row); print(json.dumps(row))
                retries += 1
                if retries >= 3:   # the same failure three times is a wall, not a hiccup
                    give_up(f"three consecutive failures; last: {str(e)[:120]}")
                reobserve()
                continue
            retries = 0
            row.update(acted=True, page_changed=st["history"][-1]["page_changed"], status=out["status"],
                       elapsed_ms=out["elapsed_ms"], url_after=st["page"].get("url"))
            log(row); print(json.dumps(row))
            if out["status"] in {"done", "blocked"}:
                break
        final = {"final": st["status"], "steps": len(st["history"]), "total_ms": round((time.perf_counter() - started) * 1000),
                 "url": st["page"].get("url"), "goal_destructive": goal_p}
        log(final); print(json.dumps(final))
        sys.exit(0 if st["status"] == "done" else 3)


if __name__ == "__main__":
    main()
