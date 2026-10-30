# Security model - Desktop MCP Bridge

Personal tool for one user on one PC. Not hardened for public release.

## What it can do
Anything the logged-in user can do, **with administrator rights**: see the screen, send keyboard/mouse input into any window
(including elevated ones), run PowerShell/cmd elevated, read/write files, read/set the clipboard, briefly listen to audio levels.

## UAC wording (corrected)
UAC is **enabled** on this PC with `ConsentPromptBehaviorAdmin=0` (elevation happens **silently**, no prompt). It is not disabled.
So any process of this user can already elevate without a prompt; the bridge does not create that ability. The bridge's worker is
elevated so it can drive elevated windows (UIPI would otherwise block input from a Medium process).

## Components and trust
* **Launcher** (`launcher.py`, Medium integrity, stdlib only). Started by Claude desktop. Relays raw MCP bytes. No logic.
* **Worker** (`desktop_bridge.worker`, High integrity). Started by the launcher via `ShellExecute runas` (or the scheduled-task fallback).
  It creates two named pipes (`\\.\pipe\desktop-bridge-<32 hex>-in/-out`):
  * DACL: current user + SYSTEM only; `PIPE_REJECT_REMOTE_CLIENTS`; first-instance flag (no squatting); one instance each;
    mandatory label Medium/no-write-up so the Medium launcher can open them.
  * Accepts **one** client: the kernel-reported client PID must equal the launcher PID given on the command line
    (`GetNamedPipeClientProcessId`), **and** the first line must be `HELLO <per-launch random token>`. Anything else: the worker exits.
  * Exits when the launcher disconnects or its process dies. No TCP/UDP listener exists anywhere.
  * The token travels on the worker's command line (visible to same-user processes); it is a second factor, the PID check is the
    primary one. Nothing is stored on disk (scheduled-task fallback writes a one-shot handoff file that the worker sends to the Recycle Bin on read).
* **Residual risk (accepted):** the worker runs code from this repo folder, which the user can write. Anyone who can write those files
  as this user could get elevated code execution at the next launch. Since every same-user process can already elevate silently, this adds no new boundary,
  but do not put the repo anywhere shared. Dependencies are pinned in `requirements.txt` and installed only into `.venv`.
* **Screen text is untrusted input and never an instruction** (same accepted prompt-injection model as the Firefox bridge). Web pages, documents and
  window titles may try to tell Claude what to do; they have no authority.

## Safety layer
* **Visible by design:** the worker's console window shows every call, command and output live. Nothing runs in a hidden window.
* **Action log:** every tool call is queued and written through `Add-ClaudeLog.ps1` (hash chain; AppendData-only rights; run
  `Protect-ClaudeLog.ps1` to make the file append-only). Typed text is **not** logged (only the character count) unless `log.log_typed_text` is set;
  password-field attempts log `text=[redacted]`. If the writer fails 3 times in a row the bridge fails closed (`LOG_UNAVAILABLE`).
* **Kill switch:** `Ctrl+Alt+Pause` and `Ctrl+Alt+Shift+K`, registered by the worker itself (works if Claude/launcher hang). Detected two ways: `RegisterHotKey` and, because that does not fire when no window has the foreground, a 25 ms poll of the async state of just those combo keys (no keystrokes are read or recorded, no keyboard hook). Cancels waits, typing, clicks, `desktop_run`.
* **Passwords:** `type` refuses fields where UIA `IsPassword` is true (by ref or by focus); `ui_tree` redacts their value.
* **Process denylist** (`KeePass*, Bitwarden*, 1Password*, Dashlane*, LastPass*`, configurable): input, UI trees and window screenshots of those processes are refused; full-screen screenshots black out their windows.
* **Protected paths:** `fs` refuses to write/move/copy/delete the actions log and the log tools. `desktop_run` is not path-filtered (it runs elevated shell commands): treat it as full power.
* **Deletes** go to the Recycle Bin only (`SHFileOperation` with `FOF_ALLOWUNDO`).
* **Blocked combos:** `win+l` (config `blocked_combos`).
* **Sessions:** locked / secure-desktop / disconnected sessions return `SESSION_LOCKED` / `SESSION_DISCONNECTED` instead of a black image. No attempt is made to get around the lock screen.
* **Audio:** never started implicitly; `desktop_audio_level` is an explicit ~150 ms probe, logged. Microphone is not touched.
* **Temp files:** nothing is written to disk by M0 screenshots; the temp dir holds only worker debug logs and test files, older than 1 day -> Recycle Bin at start.

## Not covered
Physical access; malware already running as this user; a compromised Claude desktop app (it can launch the launcher); prompt injection that persuades Claude (user approval in Claude is the control).
