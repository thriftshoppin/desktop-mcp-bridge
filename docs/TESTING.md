# Testing

| Command | What | Needs the user? |
|---|---|---|
| `.venv\Scripts\python.exe -m unittest discover tests` | Offline unit tests: coordinates, key parsing, policy and protected paths, awareness rules, client identity and allow-list, two-stage Esc decisions, cancellation, control-pipe hardening (in-process squatter pipe, read timeout, connection caps). No window, no input | no |
| `.venv\Scripts\python.exe tests\security-e2e.py` | Offline part: a squatter owns the control pipe, the real launcher must refuse it and send nothing | no |
| `.venv\Scripts\python.exe tests\security-e2e.py --live` | Also starts real elevated test guardians (own visible windows, test instance names): squatted start, 80-connection flood, forged pid, wrong session token | **yes, ask first** |
| `.venv\Scripts\python.exe selftest.py all` (or `core`, `control`, `sessions`; `--only=name` selects single guardian/Esc tests) | Drives the real launcher and an elevated guardian over MCP; opens windows on every monitor, moves the mouse and types for several minutes | **yes, ask first** |

A physical Esc press on a real keyboard is not part of any script; do it once by hand. Selftest "user" actions (Esc, minimising a window) come from a separate elevated helper (`tests/helper_elev.py`), never from the bridge.
