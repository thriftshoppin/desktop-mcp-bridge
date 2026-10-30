"""Thin ctypes/pywin32 helpers: DPI awareness, monitors, session state, windows, SendInput, clipboard.

Imports dxcam-free; safe to import early. Everything here uses physical pixels (per-monitor DPI v2).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import fnmatch
import os
import time
from typing import Callable

import psutil

from .coords import Monitor

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shcore = ctypes.WinDLL("shcore")
dwmapi = ctypes.WinDLL("dwmapi")
wtsapi32 = ctypes.WinDLL("wtsapi32")
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t


def set_dpi_aware() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


# ----------------------------------------------------------------------------- process / token

def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def integrity_level() -> str:
    """'low' | 'medium' | 'high' | 'system' | 'unknown'."""
    try:
        import win32api
        import win32con
        import win32security
        tok = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        sid, _attrs = win32security.GetTokenInformation(tok, win32security.TokenIntegrityLevel)
        rid = sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
        return "system" if rid >= 0x4000 else "high" if rid >= 0x3000 else "medium" if rid >= 0x2000 else "low"
    except Exception:
        return "unknown"

# ----------------------------------------------------------------------------- monitors

class _MONITORINFOEX(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD),
                ("szDevice", ctypes.c_wchar * 32)]


def list_monitors() -> list[Monitor]:
    import win32api
    import win32con
    found = []
    for hmon, _hdc, _rect in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hmon)
        x0, y0, x1, y1 = info["Monitor"]
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        try:
            shcore.GetDpiForMonitor(wt.HANDLE(int(hmon)), 0, ctypes.byref(dx), ctypes.byref(dy))
            dpi = dx.value or 96
        except Exception:
            dpi = 96
        try:
            hz = int(win32api.EnumDisplaySettings(info["Device"], win32con.ENUM_CURRENT_SETTINGS).DisplayFrequency)
        except Exception:
            hz = 0
        found.append(dict(name=info["Device"], x=x0, y=y0, w=x1 - x0, h=y1 - y0, dpi=dpi,
                          primary=bool(info["Flags"] & 1), hz=hz))
    found.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))
    return [Monitor(i, m["name"], m["x"], m["y"], m["w"], m["h"], m["dpi"], m["primary"], m["hz"])
            for i, m in enumerate(found)]


# ----------------------------------------------------------------------------- session

def session_info() -> dict:
    sid = wt.DWORD()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(sid))
    console_id = kernel32.WTSGetActiveConsoleSessionId()
    remote = bool(user32.GetSystemMetrics(0x1000))  # SM_REMOTESESSION
    # input desktop: if we cannot open it, or it is not "Default", the workstation is locked / on a secure desktop
    desktop = None
    h = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if h:
        try:
            n = ctypes.create_unicode_buffer(64)
            user32.GetUserObjectInformationW(h, 2, n, 128, None)
            desktop = n.value
        finally:
            user32.CloseDesktop(h)
    locked = desktop != "Default"
    # WTS connect state + client protocol (informational)
    state = None
    protocol = None
    try:
        buf = ctypes.c_void_p()
        size = wt.DWORD()
        if wtsapi32.WTSQuerySessionInformationW(None, sid.value, 8, ctypes.byref(buf), ctypes.byref(size)):  # WTSConnectState
            state = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int))[0]
            wtsapi32.WTSFreeMemory(buf)
        buf = ctypes.c_void_p()
        if wtsapi32.WTSQuerySessionInformationW(None, sid.value, 16, ctypes.byref(buf), ctypes.byref(size)):  # WTSClientProtocolType
            protocol = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ushort))[0]
            wtsapi32.WTSFreeMemory(buf)
    except Exception:
        pass
    states = {0: "active", 1: "connected", 2: "connect_query", 3: "shadow", 4: "disconnected", 5: "idle",
              6: "listen", 7: "reset", 8: "down", 9: "init"}
    is_rdp = remote or protocol == 2
    return {
        "session_id": sid.value,
        "type": "rdp" if is_rdp else "console",
        "is_active_console": sid.value == console_id,
        "wts_state": states.get(state, str(state)),
        "input_desktop": desktop,
        "locked": locked,
        "active": (state == 0) and not locked,
    }


# ----------------------------------------------------------------------------- windows

def _cloaked(hwnd) -> bool:
    v = wt.DWORD()
    try:
        dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 14, ctypes.byref(v), 4)  # DWMWA_CLOAKED
    except Exception:
        return False
    return v.value != 0


def window_rect(hwnd) -> tuple[int, int, int, int]:
    """(x, y, w, h) physical pixels, using the DWM extended frame bounds (no invisible resize borders)."""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # DWMWA_EXTENDED_FRAME_BOUNDS
        user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def window_text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(wt.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wt.HWND(hwnd), buf, n + 1)
    return buf.value


def window_class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wt.HWND(hwnd), buf, 256)
    return buf.value


def window_pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return pid.value


_proc_cache: dict[int, tuple[float, str, str]] = {}


def process_info(pid: int) -> tuple[str, str]:
    """(exe name, exe path) with a short cache; ('', '') if inaccessible."""
    now = time.monotonic()
    hit = _proc_cache.get(pid)
    if hit and now - hit[0] < 5:
        return hit[1], hit[2]
    try:
        p = psutil.Process(pid)
        name = p.name()
        try:
            path = p.exe()
        except Exception:
            path = ""
    except Exception:
        name, path = "", ""
    _proc_cache[pid] = (now, name, path)
    return name, path


def foreground_hwnd() -> int:
    return user32.GetForegroundWindow() or 0


def window_state(hwnd) -> str:
    if user32.IsIconic(wt.HWND(hwnd)):
        return "minimized"
    if user32.IsZoomed(wt.HWND(hwnd)):
        return "maximized"
    return "normal"


def list_top_windows(include_hidden_titles: bool = False) -> list[int]:
    out: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, _l):
        h = int(h) if h else 0
        if not user32.IsWindowVisible(wt.HWND(h)) or _cloaked(h):
            return True
        if not include_hidden_titles and not window_text(h) and window_class(h) not in ("Progman",):
            return True
        out.append(h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def focus_window(hwnd: int) -> bool:
    h = wt.HWND(hwnd)
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)  # SW_RESTORE
    # alt-key tap lets SetForegroundWindow succeed from a background process
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    cur = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(wt.HWND(fg), None) if fg else 0
    attached = False
    if fg_tid and fg_tid != cur:
        attached = bool(user32.AttachThreadInput(cur, fg_tid, True))
    user32.BringWindowToTop(h)
    user32.SetForegroundWindow(h)
    if attached:
        user32.AttachThreadInput(cur, fg_tid, False)
    time.sleep(0.08)
    return foreground_hwnd() == hwnd


def is_denylisted(exe_name: str, patterns: list[str]) -> bool:
    if not exe_name:
        return False
    low = exe_name.lower()
    stem = os.path.splitext(low)[0]
    return any(fnmatch.fnmatch(low, p.lower()) or fnmatch.fnmatch(stem, p.lower()) for p in patterns)


def hwnd_denylisted(hwnd: int, patterns: list[str]) -> bool:
    name, _ = process_info(window_pid(hwnd))
    return is_denylisted(name, patterns)


# ----------------------------------------------------------------------------- SendInput

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4

VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "pause": 0x13, "capslock": 0x14, "esc": 0x1B,
    "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C, "prtsc": 0x2C,
    "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E, "numlock": 0x90, "scrolllock": 0x91,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B, "meta": 0x5B, "cmd": 0x5B,
    "menu": 0x5D, "apps": 0x5D,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "volumeup": 0xAF, "volumedown": 0xAE, "mute": 0xAD, "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1,
}
for i in range(1, 25):
    VK[f"f{i}"] = 0x6F + i
for ch in "abcdefghijklmnopqrstuvwxyz":
    VK[ch] = ord(ch.upper())
for ch in "0123456789":
    VK[ch] = ord(ch)
for i in range(10):
    VK[f"num{i}"] = 0x60 + i
MODIFIERS = {"ctrl", "control", "alt", "shift", "win", "windows", "meta", "cmd"}
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90}


def parse_combo(combo: str) -> list[int]:
    toks = [t.strip().lower() for t in combo.replace(" ", "").split("+") if t.strip()]
    if combo.strip().endswith("++") or combo.strip() == "+":
        toks = [t for t in toks if t] + ["="]  # "ctrl++" -> ctrl and '='
    if not toks:
        raise ValueError("empty key combo")
    out = []
    for t in toks:
        if t not in VK:
            raise ValueError(f"unknown key '{t}'")
        out.append(VK[t])
    return out


def _send(inputs: list[_INPUT]) -> int:
    arr = (_INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _key_input(vk: int, up: bool) -> _INPUT:
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    i = _INPUT(type=1)
    i.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


def key_event(vk: int, up: bool) -> None:
    _send([_key_input(vk, up)])


def press_combo(vks: list[int], hold_ms: int = 20, check: Callable[[], None] | None = None) -> None:
    """Hold all but the last, tap the last, release in reverse. `check` raises if the kill switch fired."""
    mods, last = vks[:-1], vks[-1]
    pressed = []
    try:
        for v in mods:
            if check:
                check()
            key_event(v, False)
            pressed.append(v)
        if check:
            check()
        key_event(last, False)
        time.sleep(hold_ms / 1000)
        key_event(last, True)
    finally:
        for v in reversed(pressed):
            key_event(v, True)


def type_text(text: str, delay_ms: float = 2.0, check: Callable[[], None] | None = None) -> int:
    n = 0
    for ch in text:
        if check:
            check()
        if ch == "\n":
            key_event(0x0D, False); key_event(0x0D, True)
        elif ch == "\t":
            key_event(0x09, False); key_event(0x09, True)
        elif ch == "\r":
            continue
        else:
            for unit in _utf16_units(ch):
                down = _INPUT(type=1); down.ki = _KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0)
                up = _INPUT(type=1); up.ki = _KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
                _send([down, up])
        n += 1
        if delay_ms:
            time.sleep(delay_ms / 1000)
    return n


def _utf16_units(ch: str) -> list[int]:
    b = ch.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


MOUSE_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


def cursor_pos() -> tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def move_cursor(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def mouse_click(button: str = "left", count: int = 1, gap_ms: int = 60, check: Callable[[], None] | None = None) -> None:
    down_f, up_f = MOUSE_FLAGS[button]
    for _ in range(count):
        if check:
            check()
        d = _INPUT(type=0); d.mi = _MOUSEINPUT(0, 0, 0, down_f, 0, 0)
        u = _INPUT(type=0); u.mi = _MOUSEINPUT(0, 0, 0, up_f, 0, 0)
        _send([d])
        time.sleep(0.03)
        _send([u])
        time.sleep(gap_ms / 1000)


# ----------------------------------------------------------------------------- clipboard

def clipboard_get() -> str:
    import win32clipboard
    for _ in range(20):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("clipboard busy")


def clipboard_set(text: str) -> None:
    import win32clipboard
    for _ in range(20):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
                return
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("clipboard busy")
