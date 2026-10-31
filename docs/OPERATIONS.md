# Operations

## Run
Register `launcher.py` (run by the venv's `python.exe`) as an MCP server in your client, see the README. The first launcher starts the guardian (UAC elevation; one visible window "Desktop Bridge - live action feed"). Leave the window open: it is the bridge. Closing it stops the bridge for every client.

## Allow a client
The first time a program connects, the feed window shows `Allow client '<name>' to use the bridge? (y/N)`. Type `y` and Enter **in that window** to remember it. Anything else refuses. To revoke, delete the entry from `%ProgramData%\DesktopBridge\state\clients.json` (an administrator-owned file) or delete the file to forget all clients.

## Stop it
* **Esc** while Claude is in control: stage 1 cancels the running calls of every client; a second Esc within 10 s stops the bridge. `Ctrl+Alt+Pause` / `Ctrl+Alt+Shift+K` stop it at once. After a stop every tool answers `KILL_SWITCH` until the user tells Claude to call `desktop_resume`.
* Close the feed window, or end the guardian process.

## Injection lockout
A strong prompt-injection signature in screen/file/clipboard/command text replaces that content, locks the bridge and makes `desktop_resume` refuse. Only the user clears it by typing `y` at "Resume? (y/N)" in the feed window.

## Troubleshooting
| Symptom | Check |
|---|---|
| No window appears | `runas` refused: run `install-elevated-task.ps1` once in an elevated PowerShell (scheduled-task fallback `DesktopBridgeWorker`) |
| Launcher says "refusing to talk to the program that owns the control pipe" | Something other than this bridge's guardian owns `\.\pipe\desktop-bridge-ctl-<instance>-<user>`. Find it (`handle`/Process Explorer), close it, restart |
| Guardian window says "CONTROL PIPE UNAVAILABLE" and closes | Same cause at start-up |
| Every tool waits | The feed window is asking for approval or an "Allow client?" answer |
| Debug log | `%TEMP%\desktop-bridge\worker-<pid>.log` |
