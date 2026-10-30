# Desktop MCP Bridge (working name)

One local stdio MCP server that lets Claude use **this Windows PC** the way a person does: see the screen, read the UI tree,
click, type, press shortcuts, open apps, move files, run commands, and check whether audio is playing. Personal tool, local only,
no network listener, no push. Built for `C:\Users\SysAdmin` but paths live in `config.json`.

**Everything is visible.** The worker runs in a normal console window ("Desktop Bridge - live action feed") that prints every tool
call, every command it runs and its output, and every kill-switch event. Nothing is hidden: no `pythonw`, no `SW_HIDE`, no
`CREATE_NO_WINDOW`; helper processes share that console. Every call is also written to the actions log.

## How it is built

```
Claude desktop  --stdio-->  launcher.py (Medium integrity, stdlib only, relays bytes)
                               | 2 named pipes: user-only DACL, kernel-verified client PID, per-launch token
                               v
                      desktop_bridge.worker (ELEVATED, visible console)  -- MCP server (mcp 2.3.0 `MCPServer`)
                         capture (dxcam/DXGI, mss fallback) - UI Automation - SendInput - clipboard - audio probe
                         kill-switch hotkeys - queued action-log writer (Add-ClaudeLog.ps1)
```

* The launcher starts the worker with `ShellExecute runas`. On this PC UAC is **enabled with silent admin elevation**
  (`ConsentPromptBehaviorAdmin=0`), so there is no prompt. If `runas` ever fails, the launcher falls back to the scheduled task
  `DesktopBridgeWorker` (create it once with `install-elevated-task.ps1`).
* The worker exits when the launcher disconnects or dies. No TCP/UDP port is opened.
* Because the worker is elevated, input works in elevated windows (Task Manager, regedit, installers) and `desktop_run` /
  `desktop_launch` run elevated. The `elevated` parameters are kept for compatibility and are effectively always true.

## Install / register (you do this; the bridge never edits Claude's config)

1. **Recommended first:** run `C:\Users\SysAdmin\Claude\log-tools\Protect-ClaudeLog.ps1` yourself so the actions log becomes
   append-only and audited. The bridge writes through `Add-ClaudeLog.ps1`, which only needs AppendData rights.
2. Add this to `%LOCALAPPDATA%\Claude-Data\claude_pzs8sxrjxfjjc\claude_desktop_config.json` under `mcpServers`
   (same file and shape as the existing `firefox` entry), then restart Claude desktop:

```json
"desktop": {
  "command": "C:/Users/SysAdmin/tools/claude-desktop-bridge/.venv/Scripts/python.exe",
  "args": ["C:/Users/SysAdmin/tools/claude-desktop-bridge/launcher.py"]
}
```

   (Alternatively use the repo as a Claude plugin: `.claude-plugin/plugin.json` points at the same launcher.)
3. On first use a console window titled **Desktop Bridge - live action feed** appears. Leave it open; closing it stops the worker.
4. Optional fallback if `runas` is ever refused: from an elevated PowerShell run `install-elevated-task.ps1` once.

Rebuild the environment: `python -m venv .venv` (Python 3.14.8 here) then `.venv\Scripts\python.exe -m pip install -r requirements.txt`.

## Kill switch

**Esc** (while Claude is in control; see below), `Ctrl+Alt+Pause` or `Ctrl+Alt+Shift+K` (combos configurable in `config.json`). The worker registers them itself, so they work even if the
launcher or Claude hangs. (Hotkey plus a key-state poll, so it also works when no window has the foreground.) On trigger: in-flight waits, typing, clicks and `desktop_run` stop within about a second, every tool
returns `KILL_SWITCH` until someone calls `desktop_resume`, and the feed window shows it in red. `desktop_session_status` reports the state.

## Tools (prefix `desktop_`)

| Tool | What it does |
|---|---|
| `session_status` | console/rdp, locked, kill switch, log-writer health, worker elevation, foreground window |
| `monitors_list` | id, rect (physical px, may be negative), dpi, scale %, refresh |
| `screenshot(monitor?, region?, window?, format?)` | capped at 1568 px long edge; returns scale + origin; `SESSION_LOCKED` instead of a black image |
| `zoom(region, coords='shot'|'screen', upscale?)` | native-resolution crop, small crops upscaled up to 3x |
| `windows_list`, `window_focus` | top-level windows (z-order), `non_target` marks Progman/Lively/taskbar |
| `ui_tree(window?|point?, depth, filter, max_nodes)`, `find(query, window?, role?)` | UIA refs; default 800 nodes, `truncated` flag; passwords redacted |
| `click(ref | x,y, button, count, modifiers, coords)`, `type(text, ref?, method='keys'|'value', clear?)`, `key(combo, repeat)` | input; real mouse glide + real keystrokes; password fields refused; `win+l` blocked; own windows refused |
| `wait(seconds)` | interruptible sleep (needed for kill-switch tests; not in the original list) |
| `launch(app, args, cwd, wait_ms, user_requested_download?)`, `clipboard_get/set` | start things visibly (minimized windows restored), unicode clipboard |
| `run(command, shell, timeout, cwd, user_requested_download?)` | PowerShell or cmd, elevated, output capped at 100 KB |
| `fs(op, path, dest?, content?, ...)` | list/stat/read/write/mkdir/move/copy/delete; **delete = Recycle Bin only**; log + log tools protected |
| `audio_level(device?, probe_ms?)` | ~150 ms loopback probe of every output; reports the device with signal. Explicit and logged |
| `resume` | clear the kill switch (only when the user says so) |

### Coordinates

Screenshots are downscaled (long edge 1568). Pass `x,y` as pixels **of the last screenshot** (`coords='shot'`, the default) and the
server maps them to real pixels, including negative origins and 200 % scaling; or pass `coords='screen'` for real virtual-desktop
pixels. `ui_tree` rects are always real pixels. The mapping is a pure function in `desktop_bridge/coords.py`, unit-tested for both monitors.

### Capture engines

DXGI Desktop Duplication (`dxcam`) first; `mss` (GDI) when DXGI fails, the region spans monitors, or the session is RDP. Every
screenshot reply names the engine and why. A static screen makes DXGI deliver no new frame, so the last frame is reused and reported as `dxcam(unchanged-frame)`.

## Safety summary (details in SECURITY.md)

Append-only action log queue - kill switch - password-field refusal (`[redacted]` in the log) - process denylist
(`KeePass*, Bitwarden*, 1Password*, Dashlane*, LastPass*`, input/UI/screenshot; screenshots black out those windows) - protected log
paths - Recycle Bin deletes - temp files under `%TEMP%\desktop-bridge\` older than 1 day go to the Recycle Bin at start.

## Tests

* `.venv\Scripts\python.exe -m unittest discover tests` - coordinate mapping (both monitors, negative origin, round trips), denylist,
  key parsing, protected paths, locked-session refusal.
* `.venv\Scripts\python.exe selftest.py [all|core|control|sessions]` - drives the real launcher + elevated worker over MCP (a temp approval store is seeded only if `config.json` equals the committed version).
  **It opens visible windows on both monitors, flashes the border, moves the mouse and types for ~8 minutes; run it only when the user is at the PC and has said OK.** It needs no input from the user.
  A real physical Esc press is NOT part of it (a user must do that once by hand).

## Not in M0

`screen_watch`/60 fps recording, `av_record`/correlation, processes/services/registry/scheduled tasks/event log/system info,
`batch`, drag/scroll/hold_key/mouse_move, Show Desktop. Those are M1-M3.


## Presence, visibility and the injection gate (M0.1 - M0.3)

**You can always see and stop what Claude does.**
* **"In control" window.** From the start of any tool call (except `session_status`, `monitors_list`, `resume`) until `control.idle_grace_s` (default 8 s) after the last one.
* **Orange border + label.** While in control, every monitor gets a topmost, click-through, no-activate, ~6 px (DPI scaled: 12 px on the 200 % TV) orange (#FF8C00) border and a pill
  at the top: "Claude is using your computer · Esc to stop". It appears >= 150 ms before the first action of a burst, disappears after the grace period, turns **red**
  ("Stopped by you" / "Stopped for safety") when stopped. The overlay is excluded from the bridge's own screenshots (`WDA_EXCLUDEFROMCAPTURE`), which also hides it from
  display-capture tools; your eyes and a phone camera still see it. If it cannot be created, input tools refuse with `OVERLAY_UNAVAILABLE` (fail closed).
* **Esc stops everything.** While Claude is in control, a press of Esc halts all actions within ~50-100 ms (waits, mouse glides, typing mid-string, `run`), the border turns red and every call is refused until
  `desktop_resume`. Esc is *polled* (`GetAsyncKeyState`, every 25 ms, only while in control): no `RegisterHotKey`, no keyboard hook, so the key always reaches the app you are using.
  The bridge's own injected Esc (`desktop_key esc`) is ignored (timestamps; a real Esc within ~120 ms of it is also ignored). `Ctrl+Alt+Pause` and `Ctrl+Alt+Shift+K` still work any time.
* **The mouse is the real mouse.** Pointer actions glide the real cursor (120-300 ms, eased), draw a halo, then click. `click(ref)` clicks the element's centre with the real mouse; nothing is "invoked" silently.
* **Typing is visible.** `type` clicks the field (if you pass a ref) and types real keystrokes at ~8 ms/char. `method="value"` (ValuePattern.SetValue) is opt-in, needs a ref, only for >= 200 chars, and is announced in the feed. **This changes M0:** the old default was SetValue.
* **Self-protection.** `click/type/key/window_focus` aimed at the bridge's own feed window or overlay are refused (`SELF_TARGET_REFUSED`), so Claude cannot answer its prompts.
* **The feed window is never hidden:** it starts on the primary monitor; if minimized/hidden/off-screen when a call arrives it is restored without taking focus. `session_status.feed_window` reports it.
* **Nothing hidden:** `run` executes in the visible feed console. Refused with `HIDDEN_EXECUTION_REFUSED`: `-WindowStyle Hidden|Minimized`, `start /min|/b`, `wscript|cscript //B`, `pythonw`, `mshta`, `Start-Job`/`-AsJob`, `CreateNoWindow`, and
  scheduled-task/service creation (only with `user_requested_download=true`, as specified). Also refused: shell input synthesis (`INPUT_BYPASS_REFUSED`) and shell commands that write protected policy files (`PROTECTED_PATH`).
  `launch` opens apps in a normal window state (restores minimized windows, reports `visible_window:false` if none appeared). **These are best-effort pattern checks, not a security boundary.**
* **No background downloads.** `config.json` `downloads: "ask"`. Anything that fetches from the network (Invoke-WebRequest/iwr/irm, curl, wget, bitsadmin, certutil -urlcache, Start-BitsTransfer, WebClient/DownloadFile, pip/npm/winget/choco install,
  git clone) is refused with `DOWNLOAD_NEEDS_USER_REQUEST` unless the call passes `user_requested_download=true` (the caller sets it only when the user's own chat message asks for that download). When allowed, the feed prints it prominently and the log records `DOWNLOAD user_requested`.
  Policy: installs only via `winget` with the exact package id; otherwise the user clicks Download on the official page.

**Injection gate (no toggle).** Text returned from the screen, files, clipboard, window titles or commands is labelled `untrusted: true` and scanned for instruction-injection signatures. Strong match: the content is **withheld**, the bridge locks itself (`source=injection`), the feed shows a red alert
(tool, window/element/file, <= 120-char escaped excerpt), the log records it, and **`desktop_resume` refuses** (`INJECTION_LOCKOUT`). Only the user can resume, by typing `y` at "Resume? (y/N)" in the feed window. Weak matches are labelled and logged only.
No setting, flag or parameter relaxes this. Text *inside images* cannot be scanned (no OCR in v1).

**Config approval.** `config.json` (+ `config.local.json`) are hashed; the hash of the version you last approved is stored in `%LOCALAPPDATA%\desktop-bridge\config.approved.json`. If the config is new or changed, the feed window shows what changed and asks
`Approve changed config? (y/N)`; **no tool works until you answer**. `config.json`, `SKILL.md`, `SECURITY.md` and the approval store are on the protected-paths list (`fs` refuses writes, `run` refuses non-read-only commands touching them).

**Optional hard lock (do NOT let Claude run this; you run it yourself in an elevated PowerShell).** The worker runs as your account, elevated, so software checks can in theory be bypassed. An ACL makes accidental or casual edits impossible; it is not a boundary against an elevated process of the same account:
```
icacls "C:\Users\SysAdmin\tools\claude-desktop-bridge\config.json" /inheritance:r /grant:r "SYSTEM:(R)" "Administrators:(R)"
icacls "C:\Users\SysAdmin\tools\claude-desktop-bridge\skills" /inheritance:r /grant:r "SYSTEM:(RX)" "Administrators:(RX)" /T
icacls "C:\Users\SysAdmin\tools\claude-desktop-bridge\SECURITY.md" /inheritance:r /grant:r "SYSTEM:(R)" "Administrators:(R)"
```
To edit later: `icacls <path> /grant "Administrators:(F)"` (then re-run the lock). A real boundary needs a separate account that owns these files.

**Test hooks (can only make the bridge stricter or test a different approval store):** `DESKTOP_BRIDGE_FORCE_OVERLAY_FAIL=1` (worker `--force-overlay-fail`) and `DESKTOP_BRIDGE_APPROVAL_DIR` (worker `--approval-dir`). Neither relaxes the injection gate or any policy.
