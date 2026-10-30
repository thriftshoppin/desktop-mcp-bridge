"""On-screen presence: an orange border + label ("Claude is using your computer - Esc to stop") on every monitor, and a click halo.

Topmost, layered, click-through (WS_EX_TRANSPARENT + WM_NCHITTEST=HTTRANSPARENT), no-activate, tool-window popups, one set per
monitor. Border = 4 thin solid windows (cheap even at 4K), label = rounded pill window, halo = small per-pixel-alpha window.
Thickness is DPI scaled (6 px at 100 %, 12 px at 200 %). All windows are excluded from screen capture
(SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE) so the bridge's own screenshots do not contain them.
Everything runs on one dedicated thread with its own message loop.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time

import numpy as np

from . import winapi

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t)

u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
u32.DefWindowProcW.restype = LRESULT
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
u32.CreateWindowExW.restype = wt.HWND
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.DestroyWindow.argtypes = [wt.HWND]
u32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
u32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, ctypes.c_ubyte, wt.DWORD]
u32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
u32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, wt.BOOL]
u32.BeginPaint.argtypes = [wt.HWND, ctypes.c_void_p]
u32.BeginPaint.restype = wt.HDC
u32.EndPaint.argtypes = [wt.HWND, ctypes.c_void_p]
u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
u32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT]
u32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                    ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.c_void_p, wt.DWORD]
u32.GetDC.argtypes = [wt.HWND]
u32.GetDC.restype = wt.HDC
u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
u32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
u32.MsgWaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.c_void_p, wt.BOOL, wt.DWORD, wt.DWORD]
u32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
g32.CreateSolidBrush.argtypes = [wt.COLORREF]
g32.CreateSolidBrush.restype = wt.HBRUSH
g32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
g32.CreateRoundRectRgn.restype = wt.HRGN
g32.CreateFontW.restype = wt.HFONT
g32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
g32.SelectObject.restype = wt.HGDIOBJ
g32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
g32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
g32.CreateCompatibleDC.argtypes = [wt.HDC]
g32.CreateCompatibleDC.restype = wt.HDC
g32.DeleteDC.argtypes = [wt.HDC]
g32.DeleteObject.argtypes = [wt.HGDIOBJ]
g32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
g32.CreateDIBSection.restype = wt.HBITMAP

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x8, 0x20, 0x80, 0x80000, 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
HWND_TOPMOST = wt.HWND(-1)
WM_PAINT, WM_ERASEBKGND, WM_NCHITTEST, WM_MOUSEACTIVATE, WM_APP = 0x0F, 0x14, 0x84, 0x21, 0x8000
WDA_EXCLUDEFROMCAPTURE = 0x11

ORANGE = 0x00008CFF  # COLORREF 0x00BBGGRR for #FF8C00
RED = 0x001E1EDC     # #DC1E1E
DARK = 0x00181818

LABELS = {
    "active": "Claude is using your computer · Esc to stop",
    "stopped": "Stopped by you",
    "safety": "Stopped for safety · see the feed window",
}


def _scale(dpi: int) -> float:
    return max(dpi, 96) / 96.0


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class Overlay:
    def __init__(self, cfg: dict | None = None, force_fail: bool = False, exclude_from_capture: bool = True):
        cfg = cfg or {}
        self.base_px = int(cfg.get("thickness_px", 6))
        self.force_fail = force_fail
        self.exclude = exclude_from_capture
        self.ok = False
        self.error = ""
        self.affinity_ok = True
        self._visible = False
        self._state = "active"
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._tid = 0
        self._wins: list[dict] = []  # {hwnd, kind, mon}
        self._halo: dict | None = None
        self._halo_anim: tuple[float, int, int] | None = None  # (start, x, y)
        self._mons: list = []
        self._brush = {"orange": None, "red": None}
        self._font_cache: dict[int, int] = {}
        self._cls = "DesktopBridgeOverlay"
        self._wndproc = WNDPROC(self._proc)
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._shown_evt = threading.Event()
        self._shown_at = 0.0

    # ------------------------------------------------------------------ public (any thread)
    def start(self, timeout: float = 5.0) -> bool:
        self._thread.start()
        self._ready.wait(timeout)
        return self.ok

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def shown_at(self) -> float:
        return self._shown_at

    def hwnds(self) -> set[int]:
        return {w["hwnd"] for w in self._wins} | ({self._halo["hwnd"]} if self._halo else set())

    def show(self, timeout: float = 2.0) -> bool:
        if not self.ok:
            return False
        if self._visible:
            return True
        self._shown_evt.clear()
        self._cmd("show")
        return self._shown_evt.wait(timeout) and self._visible

    def hide(self) -> None:
        if self.ok:
            self._cmd("hide")

    def set_state(self, state: str) -> None:
        self._state = state
        if self.ok:
            self._cmd("state")

    def halo(self, x: int, y: int) -> None:
        if self.ok and self._visible:
            self._cmd("halo", x, y)

    def close(self) -> None:
        if self._tid:
            self._cmd("quit")

    # ------------------------------------------------------------------ plumbing
    def _cmd(self, *c) -> None:
        self._q.put(c)
        if self._tid:
            u32.PostThreadMessageW(self._tid, WM_APP, 0, 0)

    def _run(self) -> None:
        self._tid = k32.GetCurrentThreadId()
        try:
            if self.force_fail:
                raise RuntimeError("overlay creation forced to fail (--force-overlay-fail)")
            self._register()
            self._brush["orange"] = g32.CreateSolidBrush(ORANGE)
            self._brush["red"] = g32.CreateSolidBrush(RED)
            self._build()
            self.ok = True
        except Exception as e:  # noqa
            self.ok = False
            self.error = f"{type(e).__name__}: {e}"
        self._ready.set()
        if not self.ok:
            return
        msg = wt.MSG()
        last_top = time.monotonic()
        while True:
            animating = self._halo_anim is not None
            u32.MsgWaitForMultipleObjects(0, None, False, 15 if animating else 120, 0x04FF)
            quit_ = False
            while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0012:
                    quit_ = True
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
            while True:
                try:
                    c = self._q.get_nowait()
                except queue.Empty:
                    break
                try:
                    if c[0] == "quit":
                        quit_ = True
                    else:
                        self._do(c)
                except Exception as e:  # noqa
                    self.error = f"{type(e).__name__}: {e}"
            if quit_:
                break
            if animating:
                self._tick_halo()
            if self._visible and time.monotonic() - last_top > 0.4:  # stay above other topmost windows (Task Manager etc.)
                last_top = time.monotonic()
                for w in self._wins:
                    u32.SetWindowPos(w["hwnd"], HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        for w in self._wins:
            u32.DestroyWindow(w["hwnd"])

    def _register(self) -> None:
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = k32.GetModuleHandleW(None)
        wc.lpszClassName = self._cls
        wc.hCursor = None
        if not u32.RegisterClassExW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:  # 1410 = already exists
            raise OSError(f"RegisterClassExW failed ({ctypes.get_last_error()})")

    def _make(self, x, y, w, h, kind, mon) -> dict:
        ex = WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE
        hwnd = u32.CreateWindowExW(ex, self._cls, "DesktopBridgeOverlay", WS_POPUP, x, y, w, h, None, None, k32.GetModuleHandleW(None), None)
        if not hwnd:
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()}) for {kind} on monitor {mon.id}")
        if kind != "halo":
            u32.SetLayeredWindowAttributes(hwnd, 0, 255, 0x2)  # LWA_ALPHA
        if self.exclude and not u32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            self.affinity_ok = False
        d = {"hwnd": int(hwnd), "kind": kind, "mon": mon}
        if kind == "pill":
            r = g32.CreateRoundRectRgn(0, 0, w + 1, h + 1, h, h)
            u32.SetWindowRgn(hwnd, r, True)
        return d

    def _build(self) -> None:
        for w in self._wins:
            u32.DestroyWindow(w["hwnd"])
        self._wins = []
        self._mons = winapi.list_monitors()
        for m in self._mons:
            s = _scale(m.dpi)
            t = max(2, round(self.base_px * s))
            x, y, w, h = m.x, m.y, m.w, m.h
            for kind, rect in (("top", (x, y, w, t)), ("bottom", (x, y + h - t, w, t)), ("left", (x, y + t, t, h - 2 * t)), ("right", (x + w - t, y + t, t, h - 2 * t))):
                self._wins.append(self._make(*rect, kind, m))
            pw, ph = round(470 * s), round(34 * s)
            self._wins.append(self._make(x + (w - pw) // 2, y + t + round(6 * s), pw, ph, "pill", m))
        if self._halo is None:
            self._halo = self._make(0, 0, 8, 8, "halo", self._mons[0])

    def _do(self, c) -> None:
        if c[0] == "show":
            cur = winapi.list_monitors()
            if [(m.x, m.y, m.w, m.h, m.dpi) for m in cur] != [(m.x, m.y, m.w, m.h, m.dpi) for m in self._mons]:
                self._build()
            for w in self._wins:
                u32.SetWindowPos(w["hwnd"], HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                u32.InvalidateRect(w["hwnd"], None, True)
            self._visible = True
            self._shown_at = time.monotonic()
            self._shown_evt.set()
        elif c[0] == "hide":
            for w in self._wins:
                u32.ShowWindow(w["hwnd"], 0)
            if self._halo:
                u32.ShowWindow(self._halo["hwnd"], 0)
            self._halo_anim = None
            self._visible = False
        elif c[0] == "state":
            for w in self._wins:
                u32.InvalidateRect(w["hwnd"], None, True)
        elif c[0] == "halo":
            self._halo_anim = (time.monotonic(), int(c[1]), int(c[2]))
            self._tick_halo()

    # ------------------------------------------------------------------ halo
    def _halo_bitmap(self, size: int) -> np.ndarray:
        key = ("halo", size)
        if key in self._font_cache:
            return self._font_cache[key]
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        c = (size - 1) / 2
        d = np.hypot(xx - c, yy - c)
        r0, th = size * 0.36, max(2.0, size * 0.045)
        ring = np.clip(1.0 - np.abs(d - r0) / th, 0, 1)
        glow = np.exp(-((d - r0) ** 2) / (2 * (size * 0.07) ** 2)) * 0.35
        a = np.clip(ring + glow, 0, 1)
        bgra = np.zeros((size, size, 4), np.uint8)
        bgra[..., 0] = (0x00 * a).astype(np.uint8)   # B (premultiplied)
        bgra[..., 1] = (0x8C * a).astype(np.uint8)   # G
        bgra[..., 2] = (0xFF * a).astype(np.uint8)   # R
        bgra[..., 3] = (255 * a).astype(np.uint8)
        self._font_cache[key] = bgra
        return bgra

    def _tick_halo(self) -> None:
        if not self._halo_anim or not self._halo:
            return
        t0, x, y = self._halo_anim
        age = time.monotonic() - t0
        if age >= 0.32:
            u32.ShowWindow(self._halo["hwnd"], 0)
            self._halo_anim = None
            return
        mon = next((m for m in self._mons if m.contains(x, y)), self._mons[0])
        size = max(48, round(96 * _scale(mon.dpi)))
        bgra = self._halo_bitmap(size)
        hdc_screen = u32.GetDC(None)
        mdc = g32.CreateCompatibleDC(hdc_screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hbmp = g32.CreateDIBSection(mdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, np.ascontiguousarray(bgra).ctypes.data, size * size * 4)
        old = g32.SelectObject(mdc, hbmp)
        alpha = int(255 * (1 - age / 0.32) ** 1.5)
        pos = wt.POINT(x - size // 2, y - size // 2)
        sz = wt.SIZE(size, size)
        src = wt.POINT(0, 0)
        bf = BLENDFUNCTION(0, 0, alpha, 1)
        u32.SetWindowPos(self._halo["hwnd"], HWND_TOPMOST, pos.x, pos.y, size, size, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        u32.UpdateLayeredWindow(self._halo["hwnd"], hdc_screen, ctypes.byref(pos), ctypes.byref(sz), mdc, ctypes.byref(src), 0, ctypes.byref(bf), 2)  # ULW_ALPHA
        g32.SelectObject(mdc, old)
        g32.DeleteObject(hbmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(None, hdc_screen)

    # ------------------------------------------------------------------ window procedure
    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_NCHITTEST:
            return -1  # HTTRANSPARENT: mouse goes to whatever is underneath
        if msg == WM_MOUSEACTIVATE:
            return 3  # MA_NOACTIVATE
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_PAINT:
            w = next((x for x in self._wins if x["hwnd"] == int(hwnd)), None)
            ps = ctypes.create_string_buffer(72)
            hdc = u32.BeginPaint(hwnd, ps)
            rc = wt.RECT()
            u32.GetClientRect(hwnd, ctypes.byref(rc))
            col = "orange" if self._state == "active" else "red"
            u32.FillRect(hdc, ctypes.byref(rc), self._brush[col])
            if w and w["kind"] == "pill":
                s = _scale(w["mon"].dpi)
                key = round(16 * s)
                if key not in self._font_cache:
                    self._font_cache[key] = g32.CreateFontW(-key, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
                old = g32.SelectObject(hdc, self._font_cache[key])
                g32.SetBkMode(hdc, 1)
                g32.SetTextColor(hdc, 0x00101010 if self._state == "active" else 0x00FFFFFF)
                u32.DrawTextW(hdc, LABELS.get(self._state, LABELS["active"]), -1, ctypes.byref(rc), 0x1 | 0x4 | 0x20)  # CENTER|VCENTER|SINGLELINE
                g32.SelectObject(hdc, old)
            u32.EndPaint(hwnd, ps)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)
