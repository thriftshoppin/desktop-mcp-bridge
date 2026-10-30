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

`Ctrl+Alt+Pause` or `Ctrl+Alt+Shift+K` (configurable in `config.json`). The worker registers them itself, so they work even if the
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
| `click(ref | x,y, button, count, modifiers, coords)`, `type(text, ref?, replace?)`, `key(combo, repeat)` | input; password fields refused; `win+l` blocked |
| `wait(seconds)` | interruptible sleep (needed for kill-switch tests; not in the original list) |
| `launch(app, args, cwd, wait_ms)`, `clipboard_get/set` | start things, unicode clipboard |
| `run(command, shell, timeout, cwd)` | PowerShell or cmd, elevated, output capped at 100 KB |
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
* `.venv\Scripts\python.exe selftest.py` - drives the real launcher + elevated worker over MCP. **It opens visible windows, moves the
  mouse and types for ~1-2 minutes; keep your hands off.** It also opens/closes Task Manager and a Notepad tab.

## Not in M0

`screen_watch`/60 fps recording, `av_record`/correlation, processes/services/registry/scheduled tasks/event log/system info,
`batch`, drag/scroll/hold_key/mouse_move, Show Desktop. Those are M1-M3.
