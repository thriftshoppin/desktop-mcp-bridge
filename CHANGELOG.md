# Changelog

Work in progress; no release has been tagged yet. Dates are 2026.

## Unreleased
- **Control pipe hardening (audit DB-1, DB-2):** the launcher authenticates the owner of the control pipe (image and High integrity) before sending anything; the guardian reports a squatted control pipe loudly and stops; silent control connections time out after 5 s; connections are capped per process (4) and in total (32 unanswered).
- Docs: architecture, configuration, operations, testing, generated tool reference, security audit log, threat model and disclaimer.

## 2026-10-30 - shared guardian
- One persistent elevated guardian per Windows session shared by every MCP client; launchers are thin relays; Claude desktop is the primary client (input priority).
- One-time typed "Allow client?" allow-list, kernel-verified client pid, per-session pipe pair and one-time token, identity walk from launcher to the starting program.
- Two-stage Esc: first press cancels every in-flight call of every client, second press (within 10 s) stops the bridge.
- Prompts: typed in the visible feed console, stale typed-ahead keys discarded, withdrawn when the asker has gone; guardian refuses to run without a visible console.
- Approval store moved to `%ProgramData%\DesktopBridge\state` (a packaged app's `%LOCALAPPDATA%` is redirected).

## Earlier (M0 - M0.3)
- Takeover core: screenshot/zoom, UI Automation tree and find, real mouse and visible typing, shortcuts, launch, run, fs (Recycle Bin deletes), clipboard, audio level.
- Visibility: orange border on every monitor while in control, everything runs in the visible feed console, no hidden execution.
- Safety layer: hash-chained action log, kill switch hotkeys, process denylist, protected paths, injection gate with user-only lockout clearing, config approval, awareness guards (target closed, focus changed, user intervention), multi-monitor coordinate mapping.
