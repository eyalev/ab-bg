#!/usr/bin/env python3
"""
The portable core of ab-bg: open a URL in a NEW Chrome window that never takes
focus and is minimized immediately, over raw CDP. No agent-browser dependency.

    python3 hidden_window.py https://example.com            # prints targetId
    python3 hidden_window.py https://example.com --show     # new window, no focus steal, but visible
    CDP_PORT=9333 python3 hidden_window.py https://example.com

Three CDP calls do all the work (browser-level session, not a page session):

  1. Target.createTarget {url, newWindow: true, background: true}
       newWindow  -> its own OS window, so it never appends to the human's window
       background -> no focus steal (verified on Linux/X11; also what
                     vercel-labs/agent-browser PR #1695 proposes for tabs)
  2. Browser.getWindowForTarget {targetId}         -> windowId
  3. Browser.setWindowBounds {windowId, bounds: {windowState: "minimized"}}

A minimized window still renders off-screen: navigation, screenshots,
accessibility snapshots, Input.* clicks and typing all keep working against it.
Drive the returned targetId with whatever CDP client you already use
(Playwright's `browser.contexts()[0].pages()` will show it, Puppeteer's
`browser.targets()`, agent-browser's `tab <targetId>`, ...).

Dependency: `pip install websocket-client` (or swap in `websockets`/aiohttp;
the protocol is four JSON messages).
"""
import json
import os
import sys
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    sys.exit("pip install websocket-client")


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    url = sys.argv[1]
    minimize = "--show" not in sys.argv[2:]
    port = os.environ.get("CDP_PORT", "9222")

    version = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5))
    # suppress_origin: Chrome rejects browser-level WS connections that carry an Origin header
    ws = websocket.create_connection(version["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)

    def call(i, method, params):
        ws.send(json.dumps({"id": i, "method": method, "params": params}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == i:
                if "error" in r:
                    raise RuntimeError(r["error"])
                return r["result"]

    try:
        tid = call(1, "Target.createTarget", {"url": url, "newWindow": True, "background": True})["targetId"]
        if minimize:
            wid = call(2, "Browser.getWindowForTarget", {"targetId": tid})["windowId"]
            call(3, "Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}})
        print(tid)
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    sys.exit(main())
