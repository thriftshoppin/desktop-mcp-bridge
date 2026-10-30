"""All M0 tool implementations (plain sync functions on a Bridge object). worker.py wraps them for MCP.

Threading: `ui` pool = ONE thread with COM initialised (UIA, DXGI capture, SendInput, window calls). `misc` pool = a few
threads for run/fs/clipboard/launch/audio/wait so a long `run` never blocks the UI thread or desktop_resume.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import threading
import time

from . import capture, coords, policy, safety, uia, winapi
from .coords import Monitor
from .safety import ToolRefused

PROTECTED_CLASSES = {"Progman", "WorkerW"}
FEED_TITLE = "Desktop Bridge - live action feed"
IGNORED_WINDOW_CLASSES = {"IME", "MSCTFIME UI", "GDI+ Hook Window Class", "CicMarshalWndClass", "tooltips_class32"}


def console_root() -> int:
    """The feed window: the console window (or, under Windows Terminal, its owner window)."""
    h = winapi.kernel32.GetConsoleWindow()
    if not h:
        return 0
    r = winapi.user32.GetAncestor(h, 3)  # GA_ROOTOWNER
    return int(r or h)


class Rec:
    """Per-call record the tool fills in for the action log."""

    def __init__(self, args: dict):
        self.target = ""
        self.args = dict(args)
        self.note = ""
        self.show: list[str] = []  # extra lines for the live feed window (commands, output)


class Bridge:
    def __init__(self, cfg: dict, log: safety.LogWriter, ks: safety.KillSwitch):
        self.cfg = cfg
        self.log = log
        self.ks = ks
        self.started = time.time()
        self.cap = capture.Capturer()
        self.refs = uia.RefTable()
        self.last_shot: coords.ShotContext | None = None
        self._mons: tuple[float, list[Monitor]] | None = None
        self.denylist: list[str] = cfg.get("denylist", [])
        self.protected: list[str] = cfg.get("protected_paths", [])
        self.blocked_combos = {c.replace(" ", "").lower() for c in cfg.get("blocked_combos", ["win+l"])}
        self.log_typed = bool(cfg.get("log", {}).get("log_typed_text", False))
        self.protected = [os.path.expandvars(p) for p in self.protected]
        self.downloads_mode: str = cfg.get("downloads", "ask")
        self.overlay = None   # set by worker
        self.control = None   # set by worker
        self.say = lambda *a, **k: None  # set by worker: prints a line in the live feed window
        self.approved = threading.Event()  # config.json approval (set by worker after the user answers in the feed window)
        self.approval_info: dict = {}

    # ------------------------------------------------------------------ helpers
    def monitors(self, fresh: bool = False) -> list[Monitor]:
        now = time.monotonic()
        if fresh or self._mons is None or now - self._mons[0] > 5:
            self._mons = (now, winapi.list_monitors())
        return self._mons[1]

    def monitor_by(self, which) -> Monitor:
        mons = self.monitors()
        if which is None:
            return next(m for m in mons if m.primary)
        if isinstance(which, str):
            if which.lower() == "primary":
                return next(m for m in mons if m.primary)
            for m in mons:
                if which.lower() in m.name.lower():
                    return m
            which = int(which) if which.isdigit() else which
        for m in mons:
            if m.id == which:
                return m
        raise ToolRefused("BAD_MONITOR", f"Unknown monitor {which!r}. Valid ids: {[m.id for m in mons]}")

    def _denied(self, hwnd: int) -> str | None:
        name, _ = winapi.process_info(winapi.window_pid(hwnd))
        return name if winapi.is_denylisted(name, self.denylist) else None

    def _deny_pid(self, pid: int) -> str | None:
        name, _ = winapi.process_info(pid)
        return name if winapi.is_denylisted(name, self.denylist) else None

    def _to_real(self, x: float, y: float, space: str) -> tuple[int, int]:
        if space == "screen":
            return (round(x), round(y))
        if self.last_shot is None:
            raise ToolRefused("NO_SCREENSHOT", "No screenshot yet: take one first (desktop_screenshot), or pass coords='screen' "
                              "to use real virtual-desktop pixels.")
        return coords.shot_to_screen(self.last_shot, x, y)

    def _resolve_window(self, window) -> int | None:
        if window is None or window == "":
            return None
        if isinstance(window, int) or (isinstance(window, str) and window.isdigit()):
            h = int(window)
            if winapi.user32.IsWindow(h):
                return h
            raise ToolRefused("NO_WINDOW", f"No window with handle {window}.")
        needle = str(window).lower()
        for h in winapi.list_top_windows():
            if needle in winapi.window_text(h).lower():
                return h
        raise ToolRefused("NO_WINDOW", f"No visible window whose title contains {window!r}.")

    def _check_input_target(self, hwnd: int | None = None, pid: int | None = None) -> None:
        if pid is not None:
            bad = self._deny_pid(pid)
        else:
            h = hwnd or winapi.foreground_hwnd()
            bad = self._denied(h) if h else None
        if bad:
            raise ToolRefused("DENYLISTED", f"Input to '{bad}' is blocked by the process denylist.")

    # ------------------------------------------------------------------ feed window (never hidden or minimized)
    def feed_window_state(self) -> dict:
        h = console_root()
        if not h:
            return {"hwnd": 0, "visible": False, "minimized": False}
        return {"hwnd": h, "visible": bool(winapi.user32.IsWindowVisible(h)), "minimized": bool(winapi.user32.IsIconic(h))}

    def ensure_feed_visible(self) -> bool:
        """Restore the feed window if it was minimized/hidden or is off-screen, without taking keyboard focus. True if it had to be restored."""
        h = console_root()
        if not h:
            return False
        fixed = False
        if winapi.user32.IsIconic(h) or not winapi.user32.IsWindowVisible(h):
            winapi.user32.ShowWindow(h, 4)  # SW_SHOWNOACTIVATE
            fixed = True
        r = winapi.window_rect(h)
        if not any(coords.intersect(r, m.rect) for m in self.monitors()):
            self.place_feed()
            fixed = True
        if fixed:
            self.say("feed window was hidden/minimized/off-screen: restored", "yel")
        return fixed

    def place_feed(self) -> None:
        h = console_root()
        if not h:
            return
        m = next((m for m in self.monitors(fresh=True) if m.primary), None)
        if m is None:
            return
        w, hh = 900, 380
        x, y = m.x + m.w - w - 20, m.y + m.h - hh - 70
        winapi.user32.SetWindowPos(h, 0, x, y, w, hh, 0x10 | 0x40)  # SWP_NOACTIVATE | SWP_SHOWWINDOW

    def children_audit(self) -> list[dict]:
        """Child processes of the worker: each must share the visible feed console or own a visible window. Nothing hidden."""
        import ctypes
        import psutil
        try:
            buf = (ctypes.c_uint * 64)()
            n = winapi.kernel32.GetConsoleProcessList(buf, 64)
            on_console = set(buf[:n]) if n <= 64 else set()
            byp = winapi.windows_by_pid()
            out = []
            for c in psutil.Process().children(recursive=True):
                wins = [w for w in byp.get(c.pid, []) if w["class"] not in IGNORED_WINDOW_CLASSES]
                out.append({"pid": c.pid, "name": c.name(), "on_feed_console": c.pid in on_console,
                            "visible_windows": sum(1 for w in wins if w["visible"]), "hidden_windows": [w["class"] for w in wins if not w["visible"]]})
            return out
        except Exception as e:
            return [{"error": str(e)}]

    # ------------------------------------------------------------------ session / displays
    def session_status(self, rec: Rec) -> dict:
        s = winapi.session_info()
        fg = winapi.foreground_hwnd()
        mons = self.monitors(fresh=True)
        return {
            "ok": True, **s,
            "worker": {"pid": os.getpid(), "elevated": winapi.is_elevated(), "integrity": winapi.integrity_level(),
                       "uptime_s": round(time.time() - self.started)},
            "kill_switch": self.ks.status(),
            "log": self.log.stats(),
            "foreground": {"hwnd": fg, "title": winapi.window_text(fg), "process": winapi.process_info(winapi.window_pid(fg))[0]} if fg else None,
            "monitors": len(mons),
            "in_control": bool(self.control and self.control.in_control()),
            "idle_grace_s": self.control.grace if self.control else None,
            "border_lead_ms_last": self.control.last_lead_ms if self.control else None,
            "overlay": {"ok": bool(self.overlay and self.overlay.ok), "visible": bool(self.overlay and self.overlay.visible),
                        "excluded_from_capture": bool(self.overlay and self.overlay.exclude and self.overlay.affinity_ok),
                        "error": (self.overlay.error if self.overlay else "not created"), "state": getattr(self.overlay, "_state", None)},
            "feed_window": self.feed_window_state(),
            "config_approved": self.approved.is_set(),
            "children": self.children_audit(),
            "capture": {"engine_order": ["dxcam", "mss"], "rdp_forces": "mss"},
        }

    def monitors_list(self, rec: Rec) -> dict:
        return {"ok": True, "monitors": [
            {"id": m.id, "name": m.name, "rect": list(m.rect), "dpi": m.dpi, "scale_pct": m.scale_pct,
             "primary": m.primary, "refresh_hz": m.refresh_hz} for m in self.monitors(fresh=True)],
            "note": "rect = [x, y, w, h] in physical pixels of the virtual desktop (origin may be negative)."}

    # ------------------------------------------------------------------ screenshots
    def _redactions(self, region: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
        out = []
        for h in winapi.list_top_windows():
            if self._denied(h):
                r = winapi.window_rect(h)
                if coords.intersect(r, region):
                    out.append(r)
        return out

    def _shoot(self, rec: Rec, region: tuple[int, int, int, int], monitor_id: int, fmt: str | None, upscale: float = 1.0):
        sc = self.cfg.get("screenshot", {})
        session = winapi.session_info()
        arr, engine, note = self.cap.grab(region, self.monitors(), session)
        ctx = coords.plan_shot(region, monitor_id, int(sc.get("max_edge", 1568)), upscale)
        red = self._redactions(region)
        data, f = capture.encode(arr, ctx, fmt or sc.get("format", "jpeg"), int(sc.get("jpeg_quality", 85)), red)
        self.last_shot = ctx
        meta = {"ok": True, "monitor": monitor_id, "region": list(region), "image_size": [ctx.iw, ctx.ih], "scale": round(ctx.scale, 6),
                "origin": [ctx.rx, ctx.ry], "engine": engine, "format": f, "bytes": len(data), "session": session["type"],
                "coords": "x,y you pass to desktop_click are pixels of THIS image (coords='shot'); use coords='screen' for real pixels"}
        if note:
            meta["note"] = note
        if red:
            meta["redacted_windows"] = len(red)
        return meta, data, f

    def screenshot(self, rec: Rec, monitor=None, region=None, window=None, format=None) -> tuple[dict, bytes, str]:
        mons = self.monitors(fresh=True)
        vx0, vy0 = min(m.x for m in mons), min(m.y for m in mons)
        vx1, vy1 = max(m.x + m.w for m in mons), max(m.y + m.h for m in mons)
        if window is not None:
            h = self._resolve_window(window)
            if h is None or winapi.window_state(h) == "minimized":
                raise ToolRefused("WINDOW_MINIMIZED", "That window is minimized; focus it first (desktop_window_focus).")
            r = winapi.window_rect(h)
            rec.target = f"window {h} '{winapi.window_text(h)[:40]}'"
            bad = self._denied(h)
            if bad:
                raise ToolRefused("DENYLISTED", f"Screenshot of '{bad}' is blocked by the process denylist.")
            clipped = coords.intersect(r, (vx0, vy0, vx1 - vx0, vy1 - vy0))
            if not clipped:
                raise ToolRefused("OFFSCREEN", "The window is outside the visible desktop.")
            reg = clipped
        elif region is not None:
            x, y, w, h_ = (int(v) for v in region)
            reg = coords.intersect((x, y, w, h_), (vx0, vy0, vx1 - vx0, vy1 - vy0))
            if not reg:
                raise ToolRefused("OFFSCREEN", f"Region {region} is outside the desktop ({vx0},{vy0})-({vx1},{vy1}).")
            rec.target = f"region {list(reg)}"
        else:
            m = self.monitor_by(monitor)
            reg = m.rect
            rec.target = f"monitor {m.id}"
        centre = (reg[0] + reg[2] // 2, reg[1] + reg[3] // 2)
        owner = coords.monitor_at(mons, *centre)
        meta, data, f = self._shoot(rec, reg, owner.id if owner else -1, format)
        rec.note = f"{meta['engine']} {meta['image_size'][0]}x{meta['image_size'][1]}"
        return meta, data, f

    def zoom(self, rec: Rec, region, coords_space: str = "shot", upscale: float | None = None, format: str | None = "png"):
        if len(region) != 4:
            raise ToolRefused("BAD_ARGS", "region must be [x, y, w, h]")
        x, y, w, h = (float(v) for v in region)
        if coords_space == "screen":
            real = (round(x), round(y), max(1, round(w)), max(1, round(h)))
        else:
            if self.last_shot is None:
                raise ToolRefused("NO_SCREENSHOT", "Take a screenshot first, or pass coords='screen'.")
            real = coords.region_shot_to_screen(self.last_shot, (round(x), round(y), round(w), round(h)))
        long_edge = max(real[2], real[3])
        up = upscale if upscale else (1.0 if long_edge >= 800 else min(3.0, 800 / long_edge))
        mons = self.monitors(fresh=True)
        owner = coords.monitor_at(mons, real[0] + real[2] // 2, real[1] + real[3] // 2)
        rec.target = f"region {list(real)}"
        meta, data, f = self._shoot(rec, real, owner.id if owner else -1, format, upscale=up)
        meta["zoom"] = round(up, 3) if up != 1.0 else 1.0
        return meta, data, f

    # ------------------------------------------------------------------ windows
    def windows_list(self, rec: Rec, include_untitled: bool = False) -> dict:
        mons = self.monitors(fresh=True)
        fg = winapi.foreground_hwnd()
        out = []
        for h in winapi.list_top_windows(include_hidden_titles=include_untitled):
            cls = winapi.window_class(h)
            pid = winapi.window_pid(h)
            name, _ = winapi.process_info(pid)
            r = winapi.window_rect(h)
            owner = coords.monitor_at(mons, r[0] + r[2] // 2, r[1] + r[3] // 2)
            d = {"hwnd": h, "title": winapi.window_text(h)[:160], "class": cls, "process": name, "pid": pid, "rect": list(r),
                 "state": winapi.window_state(h), "focused": h == fg, "monitor": owner.id if owner else None}
            if cls in PROTECTED_CLASSES or name.lower().startswith("lively") or cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                d["non_target"] = True
            if winapi.is_denylisted(name, self.denylist):
                d["denylisted"] = True
            out.append(d)
        return {"ok": True, "windows": out, "count": len(out),
                "note": "Listed top-down in z-order. non_target = desktop/shell/wallpaper windows (Progman, Lively player, taskbar)."}

    def window_focus(self, rec: Rec, hwnd: int | None = None, title: str | None = None) -> dict:
        h = self._resolve_window(hwnd if hwnd is not None else title)
        if h is None:
            raise ToolRefused("BAD_ARGS", "Give hwnd or title.")
        rec.target = f"window {h} '{winapi.window_text(h)[:40]}'"
        self._check_not_self(h)
        self._check_input_target(hwnd=h)
        ok = winapi.focus_window(h)
        return {"ok": True, "focused": ok, "hwnd": h, "title": winapi.window_text(h), "foreground": winapi.foreground_hwnd(),
                **({} if ok else {"note": "Windows refused to move the foreground (focus-steal protection or the window is elevated/secure)."})}

    # ------------------------------------------------------------------ UI tree / find
    def _root_for(self, window, point, coords_space):
        import uiautomation as auto
        if point is not None:
            px, py = self._to_real(float(point[0]), float(point[1]), coords_space)
            c = auto.ControlFromPoint(px, py)
            if c is None:
                raise ToolRefused("NO_ELEMENT", f"No element at {px},{py}.")
            return c, None
        h = self._resolve_window(window)
        if h is not None:
            bad = self._denied(h)
            if bad:
                raise ToolRefused("DENYLISTED", f"UI contents of '{bad}' are blocked by the process denylist.")
            return auto.ControlFromHandle(h), h
        h = winapi.foreground_hwnd()
        if h:
            bad = self._denied(h)
            if bad:
                raise ToolRefused("DENYLISTED", f"UI contents of '{bad}' are blocked by the process denylist.")
        return uia.control_for_window(None), h or None

    def ui_tree(self, rec: Rec, window=None, point=None, coords_space: str = "shot", depth: int | None = None,
                filter: str | None = None, max_nodes: int | None = None, include_offscreen: bool = False) -> dict:
        c = self.cfg.get("ui_tree", {})
        depth = int(depth if depth is not None else c.get("depth", 6))
        max_nodes = int(max_nodes if max_nodes is not None else c.get("max_nodes", 800))
        root, h = self._root_for(window, point, coords_space)
        if self._deny_pid(root.ProcessId):
            raise ToolRefused("DENYLISTED", "UI contents of that process are blocked by the process denylist.")
        rec.target = f"window {h}" if h else "element at point"
        t0 = time.monotonic()
        tree, n, truncated = uia.walk(root, self.refs, depth, max_nodes, filter, include_offscreen, deadline=t0 + 12)
        out = {"ok": True, "nodes": n, "truncated": truncated, "depth": depth, "tree": tree,
               "elapsed_ms": round((time.monotonic() - t0) * 1000),
               "window": {"hwnd": h, "title": winapi.window_text(h) if h else None,
                          "process": winapi.process_info(root.ProcessId)[0]},
               "note": "rect = [x, y, w, h] real pixels. 'more' = children exist below the depth limit. Password values are redacted."}
        rec.note = f"{n} nodes{' (truncated)' if truncated else ''}"
        return out

    def find(self, rec: Rec, query: str, window=None, role: str | None = None, limit: int = 25) -> dict:
        root, h = self._root_for(window, None, "shot")
        if self._deny_pid(root.ProcessId):
            raise ToolRefused("DENYLISTED", "UI contents of that process are blocked by the process denylist.")
        rec.target = f"window {h}"
        t0 = time.monotonic()
        res, truncated = uia.find(root, self.refs, query, role, int(limit), deadline=t0 + 12)
        rec.note = f"{len(res)} matches"
        return {"ok": True, "matches": res, "count": len(res), "truncated": truncated,
                "elapsed_ms": round((time.monotonic() - t0) * 1000)}

    # ------------------------------------------------------------------ input (visible: real cursor glide, halo, real keystrokes)
    def _self_hwnds(self) -> set[int]:
        s = {console_root(), int(winapi.kernel32.GetConsoleWindow() or 0)}
        if self.overlay is not None:
            s |= self.overlay.hwnds()
        s.discard(0)
        return s

    def _is_self(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        root = int(winapi.user32.GetAncestor(hwnd, 2) or hwnd)  # GA_ROOT
        if root in self._self_hwnds():
            return True
        return FEED_TITLE.lower() in winapi.window_text(root).lower()

    def _check_not_self(self, hwnd: int) -> None:
        if self._is_self(hwnd):
            raise ToolRefused("SELF_TARGET_REFUSED", "Refused: that target is the bridge's own feed window or overlay. Claude may not click, type or "
                              "send keys there (it could answer the bridge's approval prompts).")

    def _element_point(self, ref: str) -> tuple[int, int, object]:
        c = self.refs.get(ref)
        bad = self._deny_pid(c.ProcessId)
        if bad:
            raise ToolRefused("DENYLISTED", f"Input to '{bad}' is blocked by the process denylist.")
        r = uia.rect_of(c)
        if not r:
            raise ToolRefused("NO_RECT", f"Element '{ref}' has no on-screen rectangle (offscreen or hidden).")
        return (r[0] + r[2] // 2, r[1] + r[3] // 2, c)

    def _pointer_click(self, px: int, py: int, button: str = "left", count: int = 1, modifiers: list[str] | None = None) -> dict:
        """The real mouse: eased glide to the point, halo on the overlay, then a real click. Never an invoke pattern."""
        import ctypes.wintypes as wt
        if button not in winapi.MOUSE_FLAGS:
            raise ToolRefused("BAD_ARGS", "button must be left, right or middle")
        count = max(1, min(int(count), 3))
        if coords.monitor_at(self.monitors(), px, py) is None:
            raise ToolRefused("OFFSCREEN", f"Point {px},{py} is not on any monitor.")
        hw = winapi.user32.WindowFromPoint(wt.POINT(px, py))
        root = int(winapi.user32.GetAncestor(hw, 2)) if hw else 0  # GA_ROOT
        if root:
            self._check_not_self(root)
            self._check_input_target(hwnd=root)
        mod_vks = []
        for mname in modifiers or []:
            if mname.lower() not in winapi.MODIFIERS:
                raise ToolRefused("BAD_ARGS", f"unknown modifier {mname!r}")
            mod_vks.append(winapi.VK[mname.lower()])
        self.ks.check()
        cc = self.cfg.get("control", {})
        ms = winapi.glide(px, py, int(cc.get("glide_ms_min", 120)), int(cc.get("glide_ms_max", 300)), self.ks.check)
        if self.overlay is not None:
            self.overlay.halo(px, py)
        time.sleep(0.03)
        held = []
        try:
            for v in mod_vks:
                winapi.key_event(v, False)
                held.append(v)
            winapi.mouse_click(button, count, check=self.ks.check)
        finally:
            for v in reversed(held):
                winapi.key_event(v, True)
        m = coords.monitor_at(self.monitors(), px, py)
        return {"ok": True, "clicked": [px, py], "monitor": m.id if m else None, "window": winapi.window_text(root)[:80] if root else None,
                "button": button, "count": count, "glide_ms": round(ms)}

    def click(self, rec: Rec, ref: str | None = None, x: float | None = None, y: float | None = None, button: str = "left",
              count: int = 1, modifiers: list[str] | None = None, coords_space: str = "shot") -> dict:
        if ref:
            px, py, c = self._element_point(ref)
            rec.target = f"{ref} {uia.role_of(c)} '{(c.Name or '')[:40]}'"
        elif x is not None and y is not None:
            px, py = self._to_real(float(x), float(y), coords_space)
            rec.target = f"point {px},{py}"
        else:
            raise ToolRefused("BAD_ARGS", "Give ref, or x and y.")
        return self._pointer_click(px, py, button, count, modifiers)

    def type_text(self, rec: Rec, text: str, ref: str | None = None, method: str = "keys", clear: bool = False) -> dict:
        """Default: a visible click on the target (if ref) and real keystrokes at a visible pace. method='value' (ValuePattern.SetValue)
        is opt-in, needs a ref, and only for long text (control.value_method_min_chars)."""
        import uiautomation as auto
        cc = self.cfg.get("control", {})
        n = len(text)
        if method not in ("keys", "value"):
            raise ToolRefused("BAD_ARGS", "method must be 'keys' (default) or 'value'")
        if method == "value":
            if not ref:
                raise ToolRefused("BAD_ARGS", "method='value' needs a ref")
            need = int(cc.get("value_method_min_chars", 200))
            if n < need:
                raise ToolRefused("VALUE_METHOD_TOO_SHORT", f"method='value' is only for long text (>= {need} chars); this is {n}. Use the default visible typing.")
        c = None
        if ref:
            px, py, c = self._element_point(ref)
            rec.target = f"{ref} {uia.role_of(c)} '{(c.Name or '')[:40]}'"
            if uia.is_password(c):
                rec.args["text"] = "[redacted]"
                raise ToolRefused("PASSWORD_FIELD", "That field is a password field; typing into it is refused.")
        else:
            fg = winapi.foreground_hwnd()
            self._check_not_self(fg)
            self._check_input_target()
            try:
                fc = auto.GetFocusedControl()
            except Exception:
                fc = None
            if fc is not None:
                rec.target = f"focused {uia.role_of(fc)} '{(fc.Name or '')[:40]}'"
                if uia.is_password(fc):
                    rec.args["text"] = "[redacted]"
                    raise ToolRefused("PASSWORD_FIELD", "The focused field is a password field; typing into it is refused.")
        rec.args["text"] = text if self.log_typed else f"[omitted {n} chars]"
        self.ks.check()
        if c is not None:
            self._pointer_click(px, py)  # visible click to focus the target
            time.sleep(0.08)
        if method == "value":
            self.say(f"type method=value: ValuePattern.SetValue of {n} chars into {rec.target} (opt-in, long text)", "yel")
            vp = c.GetValuePattern()
            if vp is None or vp.IsReadOnly:
                raise ToolRefused("NO_VALUE_PATTERN", "That element does not accept a value; use the default typing.")
            vp.SetValue(text)
            return {"ok": True, "typed": n, "method": "ValuePattern.SetValue (announced in the feed)"}
        if clear:
            winapi.press_combo(winapi.parse_combo("ctrl+a"), check=self.ks.check)
            winapi.press_combo(winapi.parse_combo("delete"), check=self.ks.check)
        per = float(cc.get("type_ms_per_char", 8))
        t0 = time.perf_counter()
        winapi.type_text(text, delay_ms=per, check=self.ks.check)
        el = (time.perf_counter() - t0) * 1000
        return {"ok": True, "typed": n, "method": "keystrokes", "ms_per_char": round(el / max(n, 1), 1)}

    def key(self, rec: Rec, combo: str, repeat: int = 1) -> dict:
        norm = combo.replace(" ", "").lower()
        if norm in self.blocked_combos:
            raise ToolRefused("BLOCKED_COMBO", f"'{combo}' is blocked (it would lock the workstation or similar). See config.json blocked_combos.")
        vks = winapi.parse_combo(combo)
        fg = winapi.foreground_hwnd()
        rec.target = f"fg '{winapi.window_text(fg)[:40]}'" if fg else "no foreground"
        self._check_not_self(fg)
        self._check_input_target()
        for _ in range(max(1, min(int(repeat), 50))):
            self.ks.check()
            winapi.press_combo(vks, check=self.ks.check)
            time.sleep(0.03)
        return {"ok": True, "pressed": combo, "repeat": repeat}

    def wait(self, rec: Rec, seconds: float) -> dict:
        seconds = max(0.0, min(float(seconds), 600.0))
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.ks.check()
            time.sleep(min(0.02, max(0.0, end - time.monotonic())))
        return {"ok": True, "waited_s": seconds}

    # ------------------------------------------------------------------ apps / clipboard
    def launch(self, rec: Rec, app: str, args: list[str] | None = None, cwd: str | None = None, wait_ms: int = 3000,
               elevated: bool = True, user_requested_download: bool = False) -> dict:
        from win32com.shell import shell, shellcon
        import win32process
        rec.target = app
        for n in policy.launch_gate(app, args, self.protected, user_requested_download, self.downloads_mode):
            self.say("!!! " + n, "yel")
            rec.note += n + " "
        params = subprocess.list2cmdline(args) if args else ""
        before = set(winapi.list_top_windows())
        info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="open", lpFile=app,
                                    lpParameters=params, lpDirectory=cwd or "", nShow=1)
        pid = None
        h = info.get("hProcess")
        if h:
            try:
                pid = win32process.GetProcessId(h)
            except Exception:
                pass
            try:
                import win32api
                win32api.CloseHandle(h)
            except Exception:
                pass
        new: list[int] = []
        end = time.monotonic() + max(0, wait_ms) / 1000
        last_change = time.monotonic()
        while time.monotonic() < end:
            self.ks.check()
            cur = [w for w in winapi.list_top_windows() if w not in before]
            if set(cur) != set(new):
                new, last_change = cur, time.monotonic()
            # done once windows of the new process exist and nothing new appeared for 0.7 s (slow apps open a console first, then the real window)
            if new and (pid is None or any(winapi.window_pid(w) == pid for w in new)) and time.monotonic() - last_change >= 0.7:
                break
            time.sleep(0.1)
        wins = []
        for w in new:
            st = winapi.window_state(w)
            restored = False
            if st == "minimized":  # apps open visibly, in a normal window state
                winapi.user32.ShowWindow(w, 4)  # SW_SHOWNOACTIVATE
                if winapi.window_state(w) == "minimized":
                    winapi.user32.ShowWindow(w, 9)  # SW_RESTORE
                st, restored = winapi.window_state(w), True
            wins.append({"hwnd": w, "title": winapi.window_text(w)[:100], "process": winapi.process_info(winapi.window_pid(w))[0],
                         "pid": winapi.window_pid(w), "state": st, "restored_from_minimized": restored})
        out = {"ok": True, "pid": pid, "visible_window": bool(wins), "new_windows": wins, "elevated": winapi.is_elevated(),
               "note": "The worker runs elevated, so launched programs are elevated too (the 'elevated' parameter is kept for compatibility)."}
        if not wins:
            out["note"] = "started with no visible window"
            self.say(f"launch {app}: started with NO visible window (pid {pid})", "yel")
        return out

    def clipboard_get(self, rec: Rec) -> dict:
        t = winapi.clipboard_get()
        rec.note = f"{len(t)} chars"
        return {"ok": True, "text": t[:200000], "chars": len(t), "truncated": len(t) > 200000}

    def clipboard_set(self, rec: Rec, text: str) -> dict:
        rec.args["text"] = f"[omitted {len(text)} chars]"
        winapi.clipboard_set(text)
        return {"ok": True, "chars": len(text)}

    # ------------------------------------------------------------------ run / fs
    def run(self, rec: Rec, command: str, shell_: str = "powershell", timeout: float = 60, cwd: str | None = None,
            elevated: bool = True, user_requested_download: bool = False) -> dict:
        for n in policy.command_gate(command, self.protected, user_requested_download, self.downloads_mode):
            self.say("!!! " + n, "yel")
            rec.note += n + " "
        shell_ = shell_.lower()
        if shell_ == "powershell":
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$OutputEncoding=[Text.Encoding]::UTF8;" + command]
        elif shell_ == "cmd":
            argv = ["cmd.exe", "/d", "/c", "chcp 65001>nul & " + command]
        else:
            raise ToolRefused("BAD_ARGS", "shell must be powershell or cmd")
        timeout = max(1.0, min(float(timeout), 3600.0))
        t0 = time.monotonic()
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd or None,
                             creationflags=0)  # shares the worker console: no hidden window
        killed_by = []
        rec.show.append("RUN [" + shell_ + "] " + command[:600])

        def cancel():
            killed_by.append("kill switch")
            _kill_tree(p.pid)

        self.ks.add_cancel(cancel)
        out = err = b""
        timed_out = False
        try:
            while True:
                try:
                    out, err = p.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if killed_by:
                        out, err = p.communicate()
                        break
                    if time.monotonic() - t0 > timeout:
                        timed_out = True
                        _kill_tree(p.pid)
                        out, err = p.communicate()
                        break
        finally:
            self.ks.remove_cancel(cancel)
        cap = 100_000
        so, se = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        rec.note = f"exit {p.returncode}"
        for label, txt in (("out", so), ("err", se)):
            for ln in txt.splitlines()[:25]:
                rec.show.append(f"  {label}| {ln[:200]}")
        res = {"ok": True, "stdout": so[:cap], "stderr": se[:cap], "exit_code": p.returncode, "timed_out": timed_out,
               "truncated": len(so) > cap or len(se) > cap, "duration_ms": round((time.monotonic() - t0) * 1000)}
        if killed_by:
            self.ks.check()
        return res

    def fs(self, rec: Rec, op: str, path: str, dest: str | None = None, content: str | None = None, encoding: str = "utf-8",
           append: bool = False, max_bytes: int = 1_048_576, create_dirs: bool = True, limit: int = 500,
           user_requested_download: bool = False) -> dict:
        op = op.lower()
        for n in policy.fs_gate([path, dest or ""], user_requested_download, self.downloads_mode):
            self.say("!!! " + n, "yel")
        path = os.path.abspath(os.path.expandvars(path))
        rec.target = path
        dest = os.path.abspath(os.path.expandvars(dest)) if dest else None
        mutating = op in ("write", "move", "copy", "delete", "mkdir")
        if mutating:
            for p in [path] + ([dest] if dest else []):
                if safety.is_protected_path(p, self.protected):
                    raise ToolRefused("PROTECTED_PATH", f"{p} is protected (actions log / log tools); the bridge never modifies it.")
        if op == "list":
            if not os.path.isdir(path):
                raise ToolRefused("NOT_A_DIR", f"{path} is not a directory.")
            ents = []
            with os.scandir(path) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        ents.append({"name": e.name, "type": "dir" if e.is_dir(follow_symlinks=False) else "file",
                                     "size": st.st_size if not e.is_dir(follow_symlinks=False) else None,
                                     "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
                    except OSError:
                        ents.append({"name": e.name, "type": "unknown"})
            ents.sort(key=lambda d: (d["type"] != "dir", d["name"].lower()))
            return {"ok": True, "path": path, "entries": ents[:limit], "count": len(ents), "truncated": len(ents) > limit}
        if op == "stat":
            if not os.path.exists(path):
                return {"ok": True, "exists": False, "path": path}
            st = os.stat(path)
            return {"ok": True, "exists": True, "path": path, "type": "dir" if os.path.isdir(path) else "file", "size": st.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}
        if op == "read":
            if not os.path.isfile(path):
                raise ToolRefused("NOT_A_FILE", f"{path} is not a file.")
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                data = f.read(max_bytes)
            if encoding == "base64":
                return {"ok": True, "path": path, "size": size, "base64": base64.b64encode(data).decode(), "truncated": size > max_bytes}
            return {"ok": True, "path": path, "size": size, "content": data.decode(encoding, "replace"), "truncated": size > max_bytes}
        if op == "write":
            if content is None:
                raise ToolRefused("BAD_ARGS", "write needs content")
            rec.args["content"] = f"[omitted {len(content)} chars]"
            if create_dirs:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            raw = base64.b64decode(content) if encoding == "base64" else content.encode(encoding)
            with open(path, "ab" if append else "wb") as f:
                f.write(raw)
            return {"ok": True, "path": path, "bytes_written": len(raw), "appended": append}
        if op == "mkdir":
            os.makedirs(path, exist_ok=True)
            return {"ok": True, "path": path}
        if op in ("move", "copy"):
            if not dest:
                raise ToolRefused("BAD_ARGS", f"{op} needs dest")
            if op == "move":
                if os.path.exists(dest) and not os.path.isdir(dest):
                    raise ToolRefused("EXISTS", f"{dest} already exists.")
                shutil.move(path, dest)
            else:
                if os.path.isdir(path):
                    shutil.copytree(path, dest)
                else:
                    shutil.copy2(path, dest)
            return {"ok": True, "from": path, "to": dest}
        if op == "delete":
            safety.recycle(path)
            return {"ok": True, "path": path, "deleted": "sent to the Recycle Bin (not permanent)"}
        raise ToolRefused("BAD_ARGS", "op must be list, stat, read, write, mkdir, move, copy or delete")

    # ------------------------------------------------------------------ audio / kill switch
    def audio_level(self, rec: Rec, device: str | None = None, probe_ms: int | None = None) -> dict:
        from . import audio
        pm = int(probe_ms or self.cfg.get("audio", {}).get("probe_ms", 150))
        res = audio.audio_level(pm, device or self.cfg.get("audio", {}).get("device"))
        rec.note = f"{res.get('device')} peak={res.get('peak')} playing={res.get('playing')}"
        return {"ok": True, **res}

    def resume(self, rec: Rec) -> dict:
        was = self.ks.resume()  # raises INJECTION_LOCKOUT when the lockout was set by the injection gate
        if self.control:
            self.control.on_resume()
        return {"ok": True, "was_active": was}


def _kill_tree(pid: int) -> None:
    try:
        import psutil
        parent = psutil.Process(pid)
        for c in parent.children(recursive=True):
            try:
                c.kill()
            except Exception:
                pass
        parent.kill()
    except Exception:
        pass
