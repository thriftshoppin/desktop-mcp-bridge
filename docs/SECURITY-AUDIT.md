# Security audit log

> Work in progress. Desktop MCP Bridge gives an AI agent control of your computer. It is experimental. It assumes the machine is not already compromised and cannot verify that. Use it at your own risk, on a machine you trust, and keep the kill switch (Esc) in reach.

Threat model: see [../SECURITY.md](../SECURITY.md). Context for every finding: UAC is on with silent administrator elevation (`ConsentPromptBehaviorAdmin=0`), so administrator-only ACLs are not a boundary against code running as the same account.

Status values: **Open** until a fix is committed *and* tested. "Verified by" names what actually ran.

## Audit 1 - October 30, 2026 (guardian build `45de55e`)

**Scope:** the shared elevated guardian (`launcher.py`, `desktop_bridge/worker.py`, `desktop_bridge/clients.py`) as pushed at `45de55e`.
**Performed by:** Claude Cowork (code review, read-only, draft 2026-10-30 18:04) and Claude Code (Phase 1 analysis 17:20; verification of each finding against the code on 2026-10-31).
**Method:** code review only. No attack round has been run yet. Planned: Round 1 level abuse, Round 2 files and shell, Round 3 OS escape (UIPI, job escape, junctions, LOLBins), plus the scripted `tests/security-e2e.py`.

| ID | Severity | Finding | Code review verdict | Fix | Verified by | Status |
|---|---|---|---|---|---|---|
| DB-1 | Medium | The launcher never authenticated the guardian: it opened the predictable control pipe and trusted whatever answered. The guardian's first-instance pipe creation could fail silently (the exception ended the control thread with no message). | **Confirmed.** `launcher.py` `try_open_control` returned the pipe without a check; `worker.py` `control_loop` called `_create_ctl_pipe(first=True)` outside any handler. | `2ef03fb`: launcher checks the pipe server (`GetNamedPipeServerProcessId`): image must be this bridge's Python interpreter (venv shim or base interpreter) and integrity High, else it refuses and sends nothing. Guardian reports a failed first-instance creation in the feed and log and exits (code 3). *Deviation from the draft:* the server's command line is not readable from a Medium process, so the check is image + integrity, not "running `--guardian`". The integrity check is the part that stops a same-user Medium squatter; an elevated squatter is out of scope. | Unit tests (`tests/test_ctlguard.py`: image/integrity decisions, in-process squatter pipe refused, squatter receives 0 bytes); `tests/security-e2e.py` offline SEC-1a/b/c (real launcher process vs. squatter: exit 1, 0 bytes, no guardian started); launcher authenticates the real running guardian (manual check). **Not yet run live:** SEC-1d (guardian refuses to run when squatted). | **Fixed - launcher side verified; guardian side verified by code and unit logic only until `--live` runs** |
| DB-2 | Medium | A silent control connection was never timed out and connections were uncapped; 255 instances, then every launcher sees "busy" (availability). | **Confirmed.** `handle_control` blocked in `ReadFile(h, 4096)` with no timeout and a thread was spawned per connection. | `2ef03fb`: request must arrive within 5 s (watchdog `CancelIoEx`), at most 4 open control connections per client process and 32 unanswered connections in total, extras are closed at once with a feed notice. *Residual:* many different same-user processes can still hold the 32 unanswered slots for 5 s at a time (nuisance, not a bypass). | Unit tests (`read_with_timeout` drops a silent client after the timeout and reads a prompt one; `ConnLimiter` per-process and total caps; one flooder holds 4 of 255 instances). **Not yet run live:** SEC-2a/b/c, SEC-3, SEC-4, SEC-5 (`security-e2e.py --live`). | **Fixed - unit-verified; live flood test pending** |
| DB-3 | Low | Prompt fatigue: one global `prompt_lock`; any process can queue "Allow client?" questions under new identities; one stray `y` is a permanent allow. | **Confirmed.** `ask_user` holds the global lock; `allow.add` after any `y`. Partly mitigated since the audit: questions are withdrawn when the asker is gone, one question per application identity, stale typed-ahead keys are discarded, and DB-2 caps connections per process. | none yet (planned: rate limit, show the parent chain, require typing the client name or a short code) | - | Open |
| DB-4 | Low (defense in depth) | Client identity is a path string, not a signer or hash. | **Confirmed.** `clients.identity_key` hashes the normalised path (version digits collapsed) and, for script hosts, only the script's **file name** (`os.path.basename`). A different program at a user-writable path of the same shape gets the same identity. | none yet (planned: Authenticode signer or executable hash for unsigned / user-writable locations) | - | Open |
| DB-5 | Low | The state folder's ACL is inherited, never set. | **Confirmed by code reading** (`policy.Approval` and `clients.AllowList` only call `os.makedirs`). ACL itself not read. | none yet (planned: explicit protected DACL at creation) | - | Open, ACL unverified |
| DB-6 | Medium | A Medium process can open the elevated workers with `PROCESS_TERMINATE` and stop the guardian (border, kill switch and feed vanish). | **Confirmed on a test guardian** (Phase 1, 2026-10-30): a Medium process terminated an elevated test guardian. Not repeated against the user's long-running guardian. | none yet (planned: restrict the process DACL to deny terminate to the user; this is defense in depth, because the same account can elevate silently) | Phase 1 live test | Open |
| DB-7 | Low | Esc blind window: the bridge ignores a real Esc within 120 ms of the bridge's own injected Esc, so a looped `key esc` keeps the suppression alive. | **Confirmed by code reading** (`safety.py` ignores Esc while `own_esc_age() <= 0.12`). Not exploited live. | none yet (planned: raw-input hardware detection, never suppress during `key esc repeat`) | - | Open |
| DB-8 | Info | Control tools share the misc thread pool with `run`/`fs`/`wait`; the overlay can be covered by another topmost window; the log writer can be starved by a process holding the log open. | **Partly confirmed.** The shared pool is real (`_executors()`: 1 UI thread + 8 misc threads). Overlay cover and log starvation were not tested. | none yet | - | Open |

### Accepted risks and open decisions

| ID | Item | Decision |
|---|---|---|
| RISK-1 | Prompt injection through screen text, images (no OCR) and files | Accepted: content is labelled untrusted, strong signatures lock the bridge, there is no relax switch; hidden or image text is not caught |
| RISK-2 | Host already compromised (malware as the user or as administrator) | Out of scope; use at your own risk (disclaimer above) |
| OPEN-1 | `ConsentPromptBehaviorAdmin` (silent elevation vs. secure-desktop consent) | User decision |
| OPEN-2 | The Windows account name and local paths appear in tracked files and history | Decided by the user: a generic local account name leaks nothing new, no sanitising or history rewrite |

### Other findings from building the guardian (not in the draft)

| Finding | Status |
|---|---|
| MSIX file redirection hid the first approval store from the elevated guardian | Fixed (store moved to `%ProgramData%\DesktopBridge\state`) |
| A stale typed-ahead `y` in the console answered a new "Allow client?" question | Fixed in `8793053` (input flushed after the question renders); the live re-test (T72c) is still pending |
| The identity walk treated a script that merely received `launcher.py` as an argument as the launcher chain | Fixed in `45de55e`, unit-tested |
| The guardian could be started without a visible console | Fixed (it refuses to run headless) |

## Secrets and personal-data scan (2026-10-31)

Scanned the tracked tree and every commit of the history for e-mail addresses, personal names, tokens (GitHub, OpenAI-style, AWS), private keys and `password`/`api_key`/`secret`/`token` assignments. Result: no secrets, tokens or e-mail addresses; the only hits are test fixtures (`selftest.py` a dummy string, `tests/forms.ps1` a comment). All 23 commits use the GitHub no-reply author address. The local account name and generic paths are present by the maintainer's decision. `config.json` contains no credential, only paths and limits.
