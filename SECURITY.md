# Security model - Desktop MCP Bridge

Personal tool for one user on one PC. Work in progress; not hardened for production use.

**Disclaimer.** Desktop MCP Bridge gives an AI agent control of your computer. It is experimental. It assumes the machine is not already compromised and cannot verify that. Use it at your own risk, on a machine you trust, and keep the kill switch (Esc) in reach.

## Threat model

**In scope** (the bridge tries to defend against these):
1. A prompt-injected or misused Claude trying to exceed its permissions through the bridge's tools.
2. A local program that races or squats the bridge's endpoints (the control pipe, the per-session pipes).
3. A second agent client with a forged identity or header.
4. Accidental leakage: the bridge has no network listener, downloads are gated, typed text is not logged by default, captures are temporary.

**Out of scope** (accepted and documented): a host that is already compromised by malware running as the user or as an administrator. With silent UAC elevation any same-account code can become administrator, so the bridge cannot defend against it, and the maintainer cannot verify that a host is clean. Hardening that raises the cost for such malware is *defense in depth*, never a boundary.

Findings and their status: [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md).

## What it can do
Anything the logged-in user can do, **with administrator rights**: see the screen, send keyboard/mouse input into any window
(including elevated ones), run PowerShell/cmd elevated, read/write files, read/set the clipboard, briefly listen to audio levels.

## UAC wording (corrected)
UAC is **enabled** on this PC with `ConsentPromptBehaviorAdmin=0` (elevation happens **silently**, no prompt). It is not disabled.
So any process of this user can already elevate without a prompt; the bridge does not create that ability. The bridge's worker is
elevated so it can drive elevated windows (UIPI would otherwise block input from a Medium process).

## Components and trust
* **Launcher** (`launcher.py`, Medium integrity, stdlib only). Started by the MCP client. Relays raw MCP bytes. No logic beyond finding the guardian.
  **Before it sends anything it authenticates the owner of the control pipe** (`GetNamedPipeServerProcessId`): the image must be this bridge's Python interpreter and the integrity High; otherwise it refuses with an error (audit DB-1).
* **Guardian** (`desktop_bridge.worker --guardian`, High integrity, ONE per Windows session). Started by the first launcher via `ShellExecute runas` (or the scheduled-task fallback); every later launcher of any client joins it.
  * **Control pipe** `\\.\pipe\desktop-bridge-ctl-<instance>-<user>`: DACL current user + SYSTEM, `PIPE_REJECT_REMOTE_CLIENTS`, first-instance flag (if another program already owns the name the guardian says so in its feed and stops). The request must be read within 5 s, at most 4 open connections per process and 32 unanswered in total (DB-2).
  * **Client identity:** the kernel-reported pid of the pipe client must equal the pid claimed in the request; the guardian walks up from the launcher to the program that started it. A program it has never seen is **not served** until the user types `y` in the feed window; the answer is remembered in `%ProgramData%\DesktopBridge\state`.
  * **Per-session pipe pair** `desktop-bridge-<32 hex>-in/-out`: user + SYSTEM DACL, first instance, one client, kernel-verified pid and a one-time token (`HELLO <token>`). No TCP/UDP listener exists anywhere.
  * Exits after `guardian.idle_exit_min` without clients or when its window is closed. It refuses to run without a visible console.
* **Residual risks (accepted):** the guardian runs code from this repo folder, which the user can write: anyone who can write those files as this user could get elevated code execution at the next start. Since every same-user process can already elevate silently when UAC is configured that way, this adds no new boundary; do not put the repo anywhere shared. Client identity is a path string, not a signer (DB-4). A same-user process may be able to terminate the elevated guardian (DB-6). Dependencies are pinned in `requirements.txt` and installed only into `.venv`.
* **Screen text is untrusted input and never an instruction.** Web pages, documents and window titles may try to tell Claude what to do; they have no authority.

## Safety layer
* **Visible by design:** the worker's console window shows every call, command and output live. Nothing runs in a hidden window.
* **Action log:** every tool call is queued and written through `Add-ClaudeLog.ps1` (hash chain; AppendData-only rights; run
  `Protect-ClaudeLog.ps1` to make the file append-only). Typed text is **not** logged (only the character count) unless `log.log_typed_text` is set;
  password-field attempts log `text=[redacted]`. If the writer fails 3 times in a row the bridge fails closed (`LOG_UNAVAILABLE`).
* **Esc / kill switch:** Esc (polled only while Claude is 'in control'; never hooked or registered), plus the combos below. **Kill switch (combos):** `Ctrl+Alt+Pause` and `Ctrl+Alt+Shift+K`, registered by the worker itself (works if Claude/launcher hang). Detected two ways: `RegisterHotKey` and, because that does not fire when no window has the foreground, a 25 ms poll of the async state of just those combo keys (no keystrokes are read or recorded, no keyboard hook). Cancels waits, typing, clicks, `desktop_run`.
* **Presence:** orange border + label on every monitor while Claude is in control; real visible mouse and typing; self-targeting of the feed window/overlay refused.
* **Injection gate:** results from the screen/files/clipboard/commands are labelled untrusted and scanned; a strong signature locks the bridge and only the user can clear it in the feed window (see README). Limits: regex signatures (paraphrases and other languages can slip through), no OCR of screenshots, no scan of what the bridge does not return.
* **Hidden-execution / download / input-bypass tripwires:** best-effort pattern checks in `run`/`launch`/`fs`, NOT a security boundary (obfuscation, encoded commands and indirect execution can evade them).
* **Config approval:** config.json changes must be approved by the user in the feed window before any tool works. An elevated process under the same account can in theory edit the approval store, the code, or kill/replace the feed prompt; real separation needs a different account.
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
