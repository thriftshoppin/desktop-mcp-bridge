# Configuration

`config.json` (committed defaults) is merged with an optional `config.local.json` (git-ignored overrides; nested objects merge key by key). The merged result is hashed; **the hash of the version you last approved is stored in `%ProgramData%\DesktopBridge\state\config.approved.json`**. If the config is new or changed, the guardian's feed window asks `Approve changed config? (y/N)` and **no tool works until you answer**. Only the user can answer (typed in that window).

| Key | Meaning | Default |
|---|---|---|
| `log.enabled`, `log.script`, `log.section` | Every tool call is written through `log.script` (a PowerShell logger with a hash chain) | script path of the maintainer's PC |
| `log.log_typed_text` | Log typed text (otherwise only the character count; password fields log `[redacted]`) | `false` |
| `log.fail_closed` | After 3 failed log writes in a row every tool returns `LOG_UNAVAILABLE` | `true` |
| `log.max_entry_chars` | Longest log entry | 700 |
| `kill_switch.hotkeys` | Direct-stop combos (registered by the guardian and polled) | `ctrl+alt+pause`, `ctrl+alt+shift+k` |
| `denylist` | Process-name globs (KeePass, Bitwarden, ...): input, UI trees and window screenshots refused, full screenshots black them out | password managers |
| `protected_paths` | `fs` refuses writes/moves/deletes there and `run` refuses non-read-only commands touching them (log, log tools, config, skills, SECURITY.md, approval store) | see file |
| `audio.device`, `audio.probe_ms` | `desktop_audio_level` probe | any device, 150 ms |
| `temp_dir`, `retention_hours` | Debug logs and test files; older ones go to the Recycle Bin at start | `%TEMP%\desktop-bridge`, 24 |
| `screenshot.max_edge`, `.format`, `.jpeg_quality` | Screenshot downscale and encoding | 1568, jpeg, 85 |
| `ui_tree.max_nodes`, `.depth` | UI tree limits | 800, 6 |
| `downloads` | `ask`: anything that fetches from the network is refused unless the call passes `user_requested_download=true` | `ask` |
| `control.idle_grace_s` | How long the "in control" border stays after the last call | 8 |
| `control.border_lead_ms` | Border appears at least this long before the first action | 150 |
| `control.glide_ms_min/max`, `type_ms_per_char`, `value_method_min_chars`, `yield_wait_s` | Mouse glide time, typing speed, `value` typing threshold, how long input yields to the user | 120/300, 8, 200, 6 |
| `overlay.color`, `.thickness_px` | Border colour and thickness (DPI scaled) | `#FF8C00`, 6 |
| `blocked_combos` | Key combinations the bridge never sends | `win+l` |
| `guardian.idle_exit_min` | Guardian exits after this many minutes without a client (0 = never) | 120 |

Paths in `config.json` are specific to the maintainer's PC; adjust `log.script` and `protected_paths` for yours (put the changes in `config.local.json`). Test-only environment variables (they can only make the bridge stricter or point at another state folder): `DESKTOP_BRIDGE_INSTANCE`, `DESKTOP_BRIDGE_APPROVAL_DIR`, `DESKTOP_BRIDGE_IDLE_EXIT_S`, `DESKTOP_BRIDGE_FORCE_OVERLAY_FAIL`.
