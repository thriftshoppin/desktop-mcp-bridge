"""'In control' window: from the start of any tool call until idle_grace_s after the last one. While in control the orange border is
shown on every monitor and Esc stops everything; outside it, the border is hidden and the user's Esc is left alone."""
from __future__ import annotations

import threading
import time

from .safety import KillSwitch, ToolRefused

# tools that are NOT 'control' (pure status / unlock): they neither show the border nor arm Esc
NO_CONTROL = {"desktop_session_status", "desktop_monitors_list", "desktop_resume"}
# tools that act on the PC: refused (fail closed) when the on-screen presence (overlay) cannot be shown
INPUT_TOOLS = {"desktop_click", "desktop_type", "desktop_key", "desktop_launch", "desktop_run", "desktop_fs", "desktop_clipboard_set",
               "desktop_window_focus"}


class Control:
    def __init__(self, cfg: dict, overlay, ks: KillSwitch, say=None):
        c = cfg.get("control", {})
        self.grace = float(c.get("idle_grace_s", 8))
        self.lead = float(c.get("border_lead_ms", 150)) / 1000.0
        self.overlay = overlay
        self.ks = ks
        self.say = say or (lambda *a, **k: None)
        self._n = 0
        self._last = 0.0
        self._lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._reaper, name="control-reaper", daemon=True).start()

    def in_control(self) -> bool:
        return self._n > 0 or (self._last > 0 and time.monotonic() - self._last < self.grace)

    def seconds_left(self) -> float:
        return max(0.0, self.grace - (time.monotonic() - self._last)) if self._last else 0.0

    def begin(self, tool: str, input_tool: bool) -> None:
        """Called on the tool's worker thread right before it runs. Shows the border with >= border_lead_ms lead."""
        with self._lock:
            self._n += 1
            self._last = time.monotonic()
        try:
            if not self.overlay.ok:
                if input_tool:
                    raise ToolRefused("OVERLAY_UNAVAILABLE", "Refused: the on-screen 'Claude is using your computer' border could not be created "
                                      f"({self.overlay.error or 'unknown error'}), so input tools are disabled (fail closed). Read-only tools still work.")
                return
            if not self.overlay.visible:
                if not self.overlay.show():
                    if input_tool:
                        raise ToolRefused("OVERLAY_UNAVAILABLE", "Refused: the border could not be shown (fail closed).")
                    return
            if not self.ks.active:
                self.overlay.set_state("active")
            wait = self.lead - (time.monotonic() - self.overlay.shown_at)
            if wait > 0:
                time.sleep(wait)
        except BaseException:
            self.end()
            raise

    def end(self) -> None:
        with self._lock:
            self._n = max(0, self._n - 1)
            self._last = time.monotonic()

    def on_trip(self, source: str) -> None:
        """Kill switch fired: border turns red and stays up until desktop_resume (or the user's approval for lockouts)."""
        if self.overlay.ok:
            self.overlay.set_state("safety" if "injection" in source else "stopped")
            self.overlay.show()

    def on_resume(self) -> None:
        with self._lock:
            self._last = time.monotonic()
        if self.overlay.ok:
            self.overlay.set_state("active")

    def _reaper(self) -> None:
        while not self._stop:
            time.sleep(0.1)
            try:
                if self.overlay.ok and self.overlay.visible and not self.ks.active and not self.in_control():
                    self.overlay.hide()
            except Exception:
                pass

    def close(self) -> None:
        self._stop = True
