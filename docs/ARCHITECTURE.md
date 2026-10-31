# Architecture (as of the shared guardian)

> Work in progress. Experimental software that gives an AI agent control of your computer. Read [../SECURITY.md](../SECURITY.md) first.

## Processes

```
MCP client (Claude desktop = primary, Claude Code, ...)        one per client app / session
        | stdio MCP
  launcher.py   (Medium integrity, Python stdlib only; a thin relay)
        | 1. control pipe  \\.\pipe\desktop-bridge-ctl-<instance>-<user>
        |    - launcher authenticates the pipe's owner first (image = this bridge's Python, integrity High)  [DB-1]
        |    - request {"op":"connect","pid":<launcher pid>}
        | 2. reply {pipe, one-time token, client label}
        | 3. per-session pipe pair  desktop-bridge-<32 hex>-in / -out   (first-instance, user+SYSTEM DACL, Medium label)
        |    - launcher sends HELLO <token>; guardian checks kernel-reported client pid == launcher pid
        v
  THE GUARDIAN   desktop_bridge.worker --guardian   (High integrity, ONE per Windows session and instance name, visible console "live action feed")
```

* **One guardian, many clients.** The first launcher that finds no guardian starts it (`runas`, or the scheduled task `DesktopBridgeWorker` as fallback); later launchers join. A named mutex keeps it single. It exits after `guardian.idle_exit_min` (default 120) without clients, or when its window is closed. It refuses to run without a visible console (no headless mode).
* **Control pipe** (`worker.py`: `_create_ctl_pipe`, `control_loop`, `handle_control`): DACL current user + SYSTEM, `PIPE_REJECT_REMOTE_CLIENTS`, 255 instances, first instance created with `FILE_FLAG_FIRST_PIPE_INSTANCE`. If that creation fails (a squatter), the guardian says so in its feed and stops (DB-1). Every connection: kernel-reported client pid, request read with a 5 s timeout, per-process cap 4 and 32 unanswered connections in total (DB-2, `ctlguard.py`).
* **Client identity** (`clients.py`): from the launcher's pid the guardian walks up to the program that started it (the launcher chain is only a Python interpreter running `launcher.py`). The identity is the normalised program path (version numbers collapsed) plus, for script hosts such as `python.exe`, the script name. A program never seen before is **not served** until the user types `y` at "Allow client ... (y/N)" in the feed window; the answer is remembered in `%ProgramData%\DesktopBridge\state\clients.json`. Known weaknesses: see SECURITY-AUDIT.md (DB-3, DB-4).
* **Sessions.** Each client gets its own MCP server session (mcp 2.3.0 `MCPServer`, `mcp_patch.py`), its own screenshot/element reference state and a label that appears in every log line (`client=<label>`). Claude desktop is the *primary* client: when two clients want the mouse or keyboard at once, the primary goes first (others wait up to 8 s; they are not refused).
* **Execution.** One shared UI thread serialises all input (mouse, keyboard, focus); a pool of 8 threads runs everything else (capture, `run`, `fs`, `wait`, ...). The guardian is elevated so that input reaches elevated windows (UIPI) and `desktop_run` runs elevated.
* **Shared services in the guardian:** overlay (orange border, `overlay.py`), Esc/kill switch (`safety.py`, `esc.py`), control state (`control.py`), screen capture (`capture.py`: DXGI via dxcam, GDI/mss fallback), UI Automation (`uia.py`), policy gates (`policy.py`), injection scanner, action-log queue (`LogWriter`).

## A tool call, step by step

1. The client sends `tools/call`; the guardian's `invoke` records the cancellation epoch, registers the call in the hub and labels it with the client.
2. If the client is not primary and the primary has an input tool running, it waits (up to 8 s).
3. Gates run in order: approval of `config.json`, kill switch, injection lockout, overlay available, policy (denylist, protected paths, hidden-execution/download/input-bypass tripwires), awareness guards (`TARGET_CLOSED`, `FOCUS_CHANGED`, `USER_INTERVENTION`).
4. The tool runs on the UI thread or the pool. Mouse/keyboard are real input; every step re-checks the kill switch and the cancellation epoch.
5. The result is scanned (untrusted text), labelled, logged (hash chain, typed text not logged) and returned. A strong injection signature replaces the content and locks the bridge until the user clears it in the feed window.

## Esc (two stages)

Esc is polled (`GetAsyncKeyState`, 25 ms) only while Claude is "in control" - there is no keyboard hook. **Stage 1**: cancel every in-flight call of every client (`CANCELLED_BY_USER`, yellow border); the bridge stays usable. **Stage 2** (a second Esc within 10 s, or Esc with nothing running): the bridge stops (red border, every tool returns `KILL_SWITCH` until the user resumes). `Ctrl+Alt+Pause` and `Ctrl+Alt+Shift+K` stop it directly. The bridge's own injected Esc is ignored (see audit DB-7).

## State and files

| What | Where |
|---|---|
| Config + local overrides | `config.json`, `config.local.json` (git-ignored) |
| Config approval, client allow-list | `%ProgramData%\DesktopBridge\state` (outside the packaged-app file redirection that hides `%LOCALAPPDATA%`) |
| Actions log | written through the configurable `log.script` (hash chain) |
| Guardian debug log, test files | `%TEMP%\desktop-bridge\worker-<pid>.log` (older than 1 day are sent to the Recycle Bin at start) |

## Windows facts the design depends on

UAC may be on with silent elevation, so same-account code can become admin and admin-only ACLs are not a boundary against it. Packaged (MSIX) apps get a private view of `%LOCALAPPDATA%`. UIPI blocks input from a Medium process into elevated windows (hence the elevated guardian). A Medium process may be able to terminate the elevated guardian (audit DB-6).
