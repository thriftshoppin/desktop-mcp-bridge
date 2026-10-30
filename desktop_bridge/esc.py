"""Two-stage Esc. Pure decision logic (unit-tested); the polling loop lives in safety.KillSwitch.

Stage 1 (cancel): Esc while tool calls are running cancels ALL in-flight calls (every client); the bridge stays usable.
Stage 2 (stop):   a second Esc within `window_s` after a cancel, or an Esc while in control with nothing to cancel, stops the bridge
                  (kill state for every client until the user resumes in the feed window).
"""
from __future__ import annotations


def decide(now: float, inflight: int, last_cancel: float | None, window_s: float) -> str:
    """-> 'cancel' | 'stop'"""
    if last_cancel is not None and now - last_cancel < window_s:
        return "stop"
    if inflight > 0:
        return "cancel"
    return "stop"


def stage(now: float, last_cancel: float | None, window_s: float, killed: bool) -> int:
    """0 = idle, 1 = a cancel happened and a second Esc would stop the bridge, 2 = stopped."""
    if killed:
        return 2
    if last_cancel is not None and now - last_cancel < window_s:
        return 1
    return 0
