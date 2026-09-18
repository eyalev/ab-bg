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
from jev_ultrafast import agent as _agent_mod
from jev_ultrafast.browser import Browser

GATE_VERSION = "ultrafast-gate-v1"


class BorrowedBrowser(Browser):
    """Their Browser on a tab that already exists -- the ab-bg session's own hidden tab.

    Same session setup as the original (device metrics, focus emulation) but no new
    target, no navigation unless a URL is given, and close() detaches instead of
    closing: the tab belongs to the ab-bg session, which cleans it up itself.
    """

    def __init__(self, target, url=None):
        from browser_harness.admin import ensure_daemon
        from browser_harness.helpers import cdp
        ensure_daemon()
        self.target = target
        self.session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        if url:
            self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.02)

    # ---- popups -----------------------------------------------------------
    # An OAuth "Continue with Google" opens a SEPARATE browser target. Neither
    # their Browser nor ab-bg's pin follows it, so the loop would keep looking at
    # the parent page saying "signing in…" while the account chooser sits in a
    # window nobody is driving. sync_target() checks after every action: a new
    # page target whose opener is the tab we drive becomes the tab we drive; when
    # it closes, we go back to the parent. Same trusted CDP input, same gate,
    # different target id — no xdotool, no screenshots, no focus change.

    def _attach(self, target):
        from browser_harness.helpers import cdp
        try:
            cdp("Target.detachFromTarget", sessionId=self.session)
        except Exception:
            pass
        self.target = target
        self.session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        try:
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        except Exception:
            pass

    def sync_target(self):
        """Follow a popup opened from the driven tab, or return from a closed one. Returns a note or None."""
        from browser_harness.helpers import cdp
        infos = cdp("Target.getTargets")["targetInfos"]
        ids = {t["targetId"] for t in infos}
        home = getattr(self, "home", None) or self.target
        self.home = home
        if self.target != home and self.target not in ids:
            self._attach(home)
            return f"popup closed; back to {home[:8]}"
        if self.target == home:
            popup = next((t for t in infos if t["type"] == "page" and t.get("openerId") == home and t["targetId"] in ids), None)
            if popup:
                self._attach(popup["targetId"])
                return f"following popup {popup['targetId'][:8]} {popup['url'][:60]}"
        return None

    def close(self):
        from browser_harness.helpers import cdp
        if self.target:
            try:
                cdp("Target.detachFromTarget", sessionId=self.session)
            except Exception:
                pass
            self.target = None
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
    ap.add_argument("--url", help="page to open; with --target, optional (stay on the tab's current page)")
    ap.add_argument("--target", help="existing CDP target id to drive (an ab-bg session's tab) instead of opening a new tab")
    ap.add_argument("--goal", required=True)
    ap.add_argument("--danger", type=float, default=0.3)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true", help="judge the goal and predict the first step; execute nothing")
    ap.add_argument("--max-steps", type=int, default=12)
    args = ap.parse_args()
    if not args.url and not args.target:
        sys.exit("--url or --target is required")
    key = os.environ.get("TYPESAFE_API_KEY") or sys.exit("TYPESAFE_API_KEY missing; nothing run")

    if args.target:
        # Their Agent builds Browser(url) itself; hand it ours for this one construction.
        _agent_mod.Browser = lambda url: BorrowedBrowser(args.target, url or None)

    started = time.perf_counter()
    with Agent(args.url or "", args.goal) as agent:
        st = agent.state

        retries = 0
        unsure_verdicts = 0

        def settle(seconds=8.0):
            # The parent of a just-closed OAuth popup is busy finishing the sign-in: it
            # redirects a moment later. Judging it at once reads "still the login page"
            # and the loop says BLOCKED. Wait for the URL or the DOM to move, bounded.
            b = st["browser"]
            try:
                before = b.evaluate("location.href + '|' + document.body.innerText.length")
            except Exception:
                before = None
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                time.sleep(0.25)
                try:
                    now = b.evaluate("location.href + '|' + document.body.innerText.length")
                    if now != before and b.evaluate("document.readyState") == "complete":
                        time.sleep(0.5); return True
                except Exception:
                    pass
            return False

        def follow():
            # Popups: switch to one that opened from our tab, or back when it closed.
            b = st["browser"]
            note = b.sync_target() if hasattr(b, "sync_target") else None
            if note:
                log({"popup": note}); print(json.dumps({"popup": note}))
                if note.startswith("popup closed"):
                    moved = settle()
                    log({"settled": moved}); print(json.dumps({"settled": moved}))
            return note

        def reobserve():
            # After a click that navigates, the document is "navigating" for a few hundred
            # ms and observe() refuses; wait it out rather than treat it as a failure.
            st["decision"] = None; st["status"] = "ready"
            for _ in range(30):
                try:
                    follow()
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
            if args.dry:
                label = next((a.get("label", choice) for a in page["actions"] if a["id"] == choice), choice)
                row = {"step": 1, "dry": True, "would_have": f"{d.get('operation')} {label}", "p": round(d["probabilities"].get(choice, 0), 3),
                       "confidence": round(d["confidence"], 3), "goal_destructive": round(goal_p, 3), "acted": False}
                log(row); print(json.dumps(row)); sys.exit(0)
            if choice in {"DONE", "BLOCKED"} and d["confidence"] < 0.5 and unsure_verdicts < 2:
                # A hesitant verdict right after a navigation is usually a page that has not
                # finished changing. Look again, twice at most, before accepting it.
                unsure_verdicts += 1
                row = {"step": step, "choice": choice, "confidence": round(d["confidence"], 3), "unsure": "re-observing"}
                log(row); print(json.dumps(row))
                time.sleep(1.5); reobserve(); continue
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
                # A popup that just closed under us (consent's Continue) throws here too; that is
                # progress, not a failure, so it does not count toward the three strikes.
                if not follow():
                    retries += 1
                    if retries >= 3:   # the same failure three times is a wall, not a hiccup
                        give_up(f"three consecutive failures; last: {str(e)[:120]}")
                reobserve()
                continue
            retries = 0
            # Did that click open a popup (OAuth)? Then the next observation must be of the popup.
            time.sleep(0.4)
            if follow():
                reobserve()
                out = {**out, "status": st["status"]}
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
