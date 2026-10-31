# Feature: presence and awareness

Orange border and "Claude is using your computer - Esc to stop" on every monitor while in control (excluded from the bridge's own screenshots), real visible mouse glide and typing, nothing hidden (`run` executes in the feed console; hidden-execution patterns refused). Awareness guards stop an action when the target window was closed (`TARGET_CLOSED`), focus moved (`FOCUS_CHANGED`) or the user is using the keyboard/mouse (`USER_INTERVENTION`); the bridge never takes focus back. Tests: `tests/test_awareness.py`, `tests/test_visible.py`, `selftest.py`.
