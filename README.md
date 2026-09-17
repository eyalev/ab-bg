# ab-bg — background browser agents that never touch your desktop

A ~180-line bash wrapper that gives each AI agent **its own hidden Chrome
window** in your already-signed-in browser, so agents can browse as you while
you keep working — no tabs appearing in your window, no focus stolen, no
flicker.

This repo exists so you (or your coding agent) can read how it's done and
build the equivalent for your own setup. The mechanism is three CDP calls;
everything else is plumbing around one specific CLI. **Point your agent at
this README** and ask it to port the idea.

## The problem

Agents that run locally keep launching browser windows and tabs in the
window you're working in, and yanking focus while they do it. The usual
proposed fixes — run the agent in a VM, in the cloud, on a spare machine —
throw away the thing that makes local browser automation useful: the agent
is *you*. Your logins, your cookies, your `localhost:3000`.

The annoyance isn't an isolation problem. It's that the tooling does a
**foreground** `Target.createTarget` (and often a `Page.bringToFront` on
every tab switch) by default. Fix that default and the distraction is gone
without giving up locality.

## The mechanism

Over a browser-level CDP session (the `webSocketDebuggerUrl` from
`/json/version`, not a page session):

```
1. Target.createTarget      {url, newWindow: true, background: true}   → targetId
2. Browser.getWindowForTarget {targetId}                                → windowId
3. Browser.setWindowBounds  {windowId, bounds: {windowState: "minimized"}}
```

- `newWindow: true` — the tab gets its own OS window, so it never lands in
  the human's window.
- `background: true` — Chrome does not focus the new target. Verified on
  Linux/X11; it's the same flag
  [vercel-labs/agent-browser PR #1695](https://github.com/vercel-labs/agent-browser/pull/1695)
  proposes for plain tabs.
- `minimized` — the window never appears on screen or in alt-tab order.
  **A minimized window still renders off-screen**: navigation, screenshots,
  accessibility snapshots, `Input.*` clicks and typing all work against it
  (verified). Chrome throttles *timers* in backgrounded windows, not CDP
  commands; if a page must run animations/timers at full speed, launch Chrome
  with `--disable-backgrounding-occluded-windows --disable-renderer-backgrounding
  --disable-background-timer-throttling`.

Then bind your agent's session to that `targetId` and every later command
runs inside the hidden window. That's it. The standalone version with no
dependencies beyond `websocket-client` is
[`examples/hidden_window.py`](examples/hidden_window.py) — lift that file if
you don't use agent-browser.

## What `ab-bg` adds around it

`ab-bg` wraps [agent-browser](https://github.com/vercel-labs/agent-browser)
(Apache-2.0, Vercel Labs) attached to a Chrome running with
`--remote-debugging-port=9222`. agent-browser's `--session` + `--pin-tab`
already keep concurrent agents off each other's tabs; what it lacks is the
own-window/background spawn above. `ab-bg` supplies that and handles the
residue:

| Concern | What the script does |
|---|---|
| Sticky binding | `--session <name> --pin-tab tab <targetId>` right after creating the window; the same `<session>` on every call keeps the pin. |
| The daemon's stray `about:blank` | agent-browser's daemon opens a spare `about:blank` when it first connects, which can grab focus. The script diffs `/json` before/after spawn and closes only blanks that appeared, and only if still blank at cleanup (never a tab someone navigated). |
| Focus restore | Records the active X window before spawn (`xdotool getactivewindow`), re-activates it if anything took focus. Belt-and-braces — `background:true` alone usually suffices. |
| Don't fight the human | After any command, if the front tab changed *and the new front tab is ours*, hand it back. If the human switched tabs themselves mid-command, leave it. |
| Cleanup | `ab-bg <s> cleanup` closes the pinned tab, the daemon, and the recorded spares. |
| Fallback | If own-window spawn fails (no `websocket-client`, CDP down), fall back to a labelled same-window tab and say so on stderr. |

```bash
ab-bg research-1 spawn https://news.ycombinator.com   # own hidden window, pinned
ab-bg research-1 snapshot                              # accessibility tree with @refs
ab-bg research-1 get text body
ab-bg research-1 click '[data-testid="login"]'
ab-bg research-1 screenshot out.png                    # works while minimized
ab-bg research-1 cleanup                               # close window + daemon
```

One unique `<session>` per agent. Env knobs: `AB_BG_CDP_PORT` (default 9222),
`AB_BG_SHOW_WINDOW=1` (own window, no focus steal, but visible — for watching
the agent work), `AB_BG_OWN_WINDOW=0` (old same-window behaviour).

## `pick` — a bounded-choice step, so the agent stops writing per-site regexes

The step that kept costing agents turns was not clicking, it was *choosing*:
which of five buttons is "Create key", which `li` is the right account in a
Google chooser, which link is the cookie wall's "reject". Each got a one-off
`innerText` regex. `pick` replaces that with one classifier call:

```bash
ab-bg s pick "open the API keys page"            # choose + trusted-click
ab-bg s pick "reject the cookie banner" --dry    # choose only, print the top three
ab-bg s pick "pick the eyalev@gmail.com account" --min 0.9
```

It is the shape of Cua's `jev-use` (trycua/cua #3916), and the safety lives in
this script, not in the model:

1. `snapshot -i` yields the interactive elements with `@refs`. **That is the
   candidate table, and the script owns it** — only refs really on the page,
   inputs and buttons first (so a cap of `--max 80` never drops the one button
   under a hundred links), plus a mandatory `abstain`.
2. TypeSafe's Jev — a model that answers typed questions with calibrated
   probabilities instead of generating text — is asked one `Choice` over those
   ids, with the intent and a 6 kB outline of the page as state. About 300 ms.
3. An id that is not in the table **fails closed** (exit 4). `abstain`, or
   confidence under `--min` (default 0.8), prints the top three candidates and
   stops (exit 3). Nothing is clicked in either case.
4. Otherwise one **trusted** `click @ref` — real CDP input, which is what OAuth
   popups and account choosers require — and a JSON result:

```json
{"intent":"go to the Ask HN section","choice":"e106","confidence":0.97,
 "top3":[{"id":"e106","p":0.97,"what":"link: ask"},…],"ref":"e106","acted":true}
```

Text only: a canvas or image-only UI has no refs and gets `abstain`. `pick` only
clicks; for a text field it focuses the field and you `fill @ref …` yourself.
Every run is appended to `~/.cache/ab-bg/pick.jsonl` with the question version
(`pick-v1`), so a wrong pick can be read back and the wording improved rather
than guessed at. The key is read from `~/.config/desk/typesafe.key`; without it
`pick` exits 2 having asked and clicked nothing.

## Porting notes

- **The CDP part is universal.** Any CDP client can do it: Playwright's
  `browser.newContext()` is a *different* thing (isolated profile, no
  logins); you want `CDPSession.send('Target.createTarget', {...})` on the
  browser connection. Puppeteer: `browser.target().createCDPSession()` then
  the same calls.
- **The X11 bits are the only Linux-specific parts** (`xdotool` focus
  capture/restore, `DISPLAY`/`XAUTHORITY` defaults at the top of the script
  — `:1` and a GDM path, edit for your machine). On Wayland there's no
  `xdotool`; drop the focus-restore block, `background:true` does the job. On
  macOS the same three CDP calls work; if you want focus-restore, use
  `osascript` to record/re-activate the front app.
- **Don't use `Page.bringToFront` or `Target.activateTarget` anywhere in the
  agent path.** They are the things you're avoiding. Driving a hidden tab is
  fully correct without them — `Runtime.evaluate`, `Page.captureScreenshot`,
  `Input.dispatchMouseEvent` don't need foreground.
- **Prefer `targetId` over any per-daemon tab handle.** agent-browser's
  `t<N>` ids and user labels die with the daemon; the CDP `targetId` is the
  only handle that survives a restart, and it's what `tab list --json`
  reports.

## Pitfalls we hit (so your agent doesn't)

- **JS `.click()` is an untrusted event and silently breaks logins.** An OAuth
  popup (Google sign-in, etc.) will not open from `element.click()` in
  `Runtime.evaluate`; the page just reports a vague login error. Use a
  trusted click — real `Input.dispatchMouseEvent` at the element's
  coordinates (agent-browser's `click <selector>` does this). And the popup
  is a **separate CDP target**: poll `/json` for the new `accounts.google.com`
  page and drive it by its own targetId, quickly, before it closes.
- **`tab list` shows tabs from all windows, including the human's.** Never
  switch to one you didn't create.
- **Something else may be squatting your CDP port.** On a machine with an
  Android emulator, `adb forward tcp:9222` grabs `127.0.0.1:9222` and Chrome
  silently binds only `[::1]:9222` — you end up driving the phone's Chrome
  (logged out, Android UA). `ss -ltnp | grep 9222` before you blame the
  script.
- **Two Chromes, two worlds.** A Chrome launched with
  `--remote-debugging-port` and a separate `--user-data-dir` has its own
  cookies. Same-named profiles in the daily Chrome are not the same data.
  Log in once in the debug Chrome; it persists.
- **Multiple agents on the same site share one cookie jar.** Fine for reading;
  if two agents need conflicting logins to the same site, that's a second
  Chrome/profile, not a second window.

## What this does not solve

- **Isolation.** The agent runs as you, in your browser. That's the point,
  and also the risk — an agent with a trusted click can do anything you can.
  Read-only by default; confirm before anything that posts, sends, buys.
- **Non-browser pop-ups.** An agent deciding to open a GUI text editor or
  another app is the same problem one level up. Same fix in spirit — make
  every agent launch non-focus-stealing and off the working window — but
  that's `EDITOR=`/`VISUAL=` hygiene in the agent's environment and, in the
  general case, giving the agent's process tree its own virtual desktop.
  Not in this repo.

## Requirements

- Chrome/Chromium started with `--remote-debugging-port=9222` (ideally a
  dedicated `--user-data-dir` you sign into once).
- [agent-browser](https://github.com/vercel-labs/agent-browser) ≥ 0.34
  (`npm i -g agent-browser`) — for `ab-bg` itself; not needed for the example.
- `python3` + `websocket-client` (`pip install websocket-client`), `jq`,
  `curl`; `xdotool` on X11 (optional, focus-restore only).

Install: `cp ab-bg ~/.local/bin/ && chmod +x ~/.local/bin/ab-bg`.

## Status

Works daily on Linux/X11 driving a signed-in Chrome with many concurrent
agents. Upstream agent-browser (0.37 at time of writing) does not have
own-window/background spawn; PR #1695 covers the `background:true` half for
tabs and is open. If it lands, most of this script collapses to the
`newWindow` + `minimized` calls.

MIT.
