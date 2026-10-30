"""Elevated test helper, started by selftest.py (visible console, silent runas). It plays the part of the USER (or of other software),
not of the bridge: it presses Esc at an exact time, focuses or minimizes the bridge's feed window.
Usage: helper_elev.py <action> --at <epoch seconds> --out <file> [--hold ms] [--count n] [--gap ms]
Actions: esc | focus_feed | minimize_feed
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")
FEED = "Desktop Bridge - live action feed"


class KI(ctypes.Structure):
    _fields_ = [("vk", wt.WORD), ("sc", wt.WORD), ("fl", wt.DWORD), ("t", wt.DWORD), ("x", ctypes.c_size_t)]


class MI(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("md", wt.DWORD), ("fl", wt.DWORD), ("t", wt.DWORD), ("x", ctypes.c_size_t)]


class U(ctypes.Union):
    _fields_ = [("ki", KI), ("mi", MI)]


class IN(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", U)]


def key(vk, up):
    i = IN(type=1)
    i.ki = KI(vk, user32.MapVirtualKeyW(vk, 0), 2 if up else 0, 0, 0)
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(IN))


def feed_hwnd():
    found = []
    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, _):
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if FEED.lower() in buf.value.lower() and user32.IsWindowVisible(h):
            found.append(int(h))
        return True

    user32.EnumWindows(PROC(cb), 0)
    return found[0] if found else 0


ap = argparse.ArgumentParser()
ap.add_argument("action")
ap.add_argument("--at", type=float, default=0)
ap.add_argument("--out", default="")
ap.add_argument("--hold", type=int, default=40)
ap.add_argument("--count", type=int, default=1)
ap.add_argument("--gap", type=int, default=300)
ap.add_argument("--title", default="")
ap.add_argument("--vk", type=int, default=0)
ap.add_argument("--pid", type=int, default=0)
ap.add_argument("--text", default="")
ap.add_argument("--x", type=int, default=0)
ap.add_argument("--y", type=int, default=0)
a = ap.parse_args()
while time.time() < a.at:
    time.sleep(0.001)
res = {"action": a.action, "t_start": time.time()}
if a.action == "esc":
    for i in range(a.count):
        res.setdefault("t_down", []).append(time.time())
        key(0x1B, False)
        time.sleep(a.hold / 1000)
        key(0x1B, True)
        time.sleep(a.gap / 1000)
elif a.action == "close_window":
    found = []
    PROC2 = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb2(h, _):
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if a.title.lower() in buf.value.lower() and user32.IsWindowVisible(h):
            found.append(int(h))
        return True

    user32.EnumWindows(PROC2(cb2), 0)
    res["closed"] = [h for h in found if user32.PostMessageW(h, 0x10, 0, 0)]  # WM_CLOSE, like the user clicking the X
elif a.action == "console_type":
    # AttachConsole(pid) + WriteConsoleInputW: the characters land in THAT process's console input buffer only (no focus, no other window involved)
    class KEYREC(ctypes.Structure):
        _fields_ = [("bKeyDown", wt.BOOL), ("wRepeatCount", wt.WORD), ("wVirtualKeyCode", wt.WORD), ("wVirtualScanCode", wt.WORD), ("uChar", wt.WCHAR), ("dwControlKeyState", wt.DWORD)]

    class INREC(ctypes.Structure):
        _fields_ = [("EventType", wt.WORD), ("pad", wt.WORD), ("Event", KEYREC)]  # 20 bytes, exactly like INPUT_RECORD

    kernel32.FreeConsole()
    ok = bool(kernel32.AttachConsole(a.pid))
    res["attached"] = ok
    if ok:
        h = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        recs = []
        for ch in a.text.replace("\\r", "\r"):
            for down in (True, False):
                r = INREC()
                r.EventType = 1
                r.Event = KEYREC(down, 1, 0x0D if ch == "\r" else 0, 0, ch, 0)
                recs.append(r)
        arr = (INREC * len(recs))(*recs)
        n = wt.DWORD()
        res["written"] = bool(kernel32.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(n)))
        res["records"] = n.value
        kernel32.FreeConsole()
elif a.action == "hold_key":
    key(a.vk, False)
    time.sleep(a.hold / 1000)
    key(a.vk, True)
elif a.action == "move_mouse":
    user32.SetCursorPos(a.x, a.y)
    res["cursor"] = [a.x, a.y]
elif a.action in ("focus_feed", "minimize_feed"):
    h = feed_hwnd()
    res["hwnd"] = h
    if a.action == "focus_feed":
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.ShowWindow(h, 9)
        user32.SetForegroundWindow(h)
        time.sleep(0.2)
        res["foreground_is_feed"] = int(user32.GetForegroundWindow()) == h
    else:
        user32.ShowWindow(h, 6)  # SW_MINIMIZE
        time.sleep(0.2)
        res["minimized"] = bool(user32.IsIconic(h))
res["t_end"] = time.time()
if a.out:
    open(a.out, "w").write(json.dumps(res))
time.sleep(0.3)
