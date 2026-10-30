"""Safety layer: queued action-log writer, kill switch, path protection, temp retention."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import queue
import re
import subprocess
import threading
import time
from typing import Callable


class ToolRefused(Exception):
    """Raised by a tool to return a clean {ok:false,error:{code,message}} to the client."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------------- action log

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_log_text(s: str, limit: int) -> str:
    s = _CTRL.sub(" ", s)
    s = s.encode("ascii", "backslashreplace").decode("ascii")  # PowerShell stdin is not UTF-8 safe
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 12] + "...[cut]"


class LogWriter:
    """Background writer. Every entry goes through Add-ClaudeLog.ps1 unchanged (hash chain, append-only rights).

    One persistent powershell child runs the script per queued line (about 30-60 ms each instead of 250 ms cold),
    falling back to a cold `powershell -File` call if the persistent child breaks.
    """

    def __init__(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled", True))
        self.script = cfg["script"]
        self.section = cfg.get("section", "")
        self.limit = int(cfg.get("max_entry_chars", 700))
        self.fail_closed = bool(cfg.get("fail_closed", True))
        self.q: queue.Queue = queue.Queue()
        self.queued = 0
        self.written = 0
        self.failed = 0
        self.consecutive_failures = 0
        self.last_error = ""
        self._ps: subprocess.Popen | None = None
        self._first = True
        self._stop = False
        self._idle = threading.Event()
        self._idle.set()
        self._t = threading.Thread(target=self._run, name="log-writer", daemon=True)
        if self.enabled:
            self._t.start()

    @property
    def healthy(self) -> bool:
        return (not self.enabled) or self.consecutive_failures < 3

    def log(self, text: str) -> None:
        if not self.enabled:
            return
        self.queued += 1
        self._idle.clear()
        self.q.put(clean_log_text(text, self.limit))

    def flush(self, timeout: float = 20.0) -> bool:
        if not self.enabled:
            return True
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.q.empty() and self._idle.is_set():
                return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        self.flush()
        self._stop = True
        self.q.put(None)
        if self._ps and self._ps.poll() is None:
            try:
                self._ps.stdin.close()
                self._ps.wait(3)
            except Exception:
                self._ps.kill()

    def stats(self) -> dict:
        return {"enabled": self.enabled, "queued": self.queued, "written": self.written, "failed": self.failed,
                "pending": self.q.qsize(), "healthy": self.healthy, "last_error": self.last_error}

    # --- internals
    def _start_ps(self) -> None:
        self._ps = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="ascii",
            errors="replace", creationflags=0x08000000)  # CREATE_NO_WINDOW

    def _write_persistent(self, text: str) -> bool:
        if self._ps is None or self._ps.poll() is not None:
            self._start_ps()
        t = text.replace("'", "''")
        sec = ""
        if self._first and self.section:
            sec = f" -Section '{self.section}'"
        marker = f"__LOGDONE_{self.written + self.failed}__"
        cmd = (f"try {{ & '{self.script}' -Text '{t}'{sec} | Out-Null; Write-Output '{marker}OK' }} "
               f"catch {{ Write-Output ('{marker}ERR ' + $_.Exception.Message) }}\n")
        self._ps.stdin.write(cmd)
        self._ps.stdin.flush()
        while True:
            line = self._ps.stdout.readline()
            if not line:
                raise RuntimeError("powershell exited")
            if marker in line:
                if f"{marker}OK" in line:
                    return True
                raise RuntimeError(line.split(marker, 1)[1].strip()[:200])

    def _write_cold(self, text: str) -> None:
        args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.script, "-Text", text]
        if self._first and self.section:
            args += ["-Section", self.section]
        r = subprocess.run(args, capture_output=True, text=True, timeout=30, creationflags=0x08000000)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout)[:200])

    def _run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                break
            try:
                try:
                    self._write_persistent(item)
                except Exception as e1:
                    try:
                        if self._ps and self._ps.poll() is None:
                            self._ps.kill()
                    except Exception:
                        pass
                    self._ps = None
                    self._write_cold(item)
                self._first = False
                self.written += 1
                self.consecutive_failures = 0
            except Exception as e:
                self.failed += 1
                self.consecutive_failures += 1
                self.last_error = str(e)[:200]
            if self.q.empty():
                self._idle.set()


# ----------------------------------------------------------------------------- kill switch

class KillSwitchActive(ToolRefused):
    def __init__(self, since: float, source: str):
        super().__init__("KILL_SWITCH", f"Kill switch is active (tripped by {source}); call desktop_resume to continue.")


class KillSwitch:
    def __init__(self, hotkeys: list[str], on_trip: Callable[[str], None] | None = None):
        from .winapi import MODIFIERS, VK, user32  # noqa
        self._user32 = user32
        self.combos = hotkeys
        self.active = False
        self.since = 0.0
        self.source = ""
        self.trips = 0
        self.registered: dict[str, bool] = {}
        self.errors: dict[str, str] = {}
        self._on_trip = on_trip
        self._cancel: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="kill-hotkeys", daemon=True)
        self._ready = threading.Event()
        self._tid = 0
        self._thread.start()
        self._ready.wait(3)

    def check(self) -> None:
        if self.active:
            raise KillSwitchActive(self.since, self.source)

    def add_cancel(self, fn: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._cancel.append(fn)
        return fn

    def remove_cancel(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if fn in self._cancel:
                self._cancel.remove(fn)

    def trip(self, source: str) -> None:
        with self._lock:
            first = not self.active
            self.active = True
            if first:
                self.since = time.time()
                self.source = source
            self.trips += 1
            cancels = list(self._cancel)
        for fn in cancels:
            try:
                fn()
            except Exception:
                pass
        if first and self._on_trip:
            self._on_trip(source)

    def resume(self) -> bool:
        with self._lock:
            was = self.active
            self.active = False
            self.source = ""
        return was

    def status(self) -> dict:
        return {"active": self.active, "source": self.source, "since": self.since or None, "trips": self.trips,
                "hotkeys": {c: self.registered.get(c, False) for c in self.combos}, "errors": self.errors}

    def close(self) -> None:
        if self._tid:
            self._user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT

    def _loop(self) -> None:
        from .winapi import VK, kernel32, parse_combo
        user32 = self._user32
        self._tid = kernel32.GetCurrentThreadId()
        ids: dict[int, str] = {}
        MOD = {0x11: 0x0002, 0x12: 0x0001, 0x10: 0x0004, 0x5B: 0x0008}
        for i, combo in enumerate(self.combos, start=1):
            try:
                vks = parse_combo(combo)
                mods = 0
                for v in vks[:-1]:
                    mods |= MOD[v]
                if user32.RegisterHotKey(None, i, mods | 0x4000, vks[-1]):  # MOD_NOREPEAT
                    ids[i] = combo
                    self.registered[combo] = True
                else:
                    self.registered[combo] = False
                    self.errors[combo] = f"RegisterHotKey failed (error {ctypes.get_last_error() or kernel32.GetLastError()})"
            except Exception as e:
                self.registered[combo] = False
                self.errors[combo] = str(e)
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam in ids:  # WM_HOTKEY
                self.trip(f"hotkey {ids[msg.wParam]}")
        for i in ids:
            user32.UnregisterHotKey(None, i)


# ----------------------------------------------------------------------------- paths & retention

def is_protected_path(path: str, protected: list[str]) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    for q in protected:
        qq = os.path.normcase(os.path.abspath(q))
        if p == qq or p.startswith(qq + os.sep):
            return True
    return False


def recycle(path: str) -> None:
    """Send a file or folder to the Recycle Bin (never a permanent delete)."""
    from win32com.shell import shell, shellcon
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_NOERRORUI | shellcon.FOF_SILENT
    rc, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, path + "\0", None, flags, None, None))
    if rc != 0 or aborted or os.path.exists(path):
        raise OSError(f"could not send to Recycle Bin (code {rc})")


def clear_old_temp(temp_dir: str, hours: float) -> int:
    """Send files older than `hours` under temp_dir to the Recycle Bin. Returns count."""
    if not os.path.isdir(temp_dir):
        return 0
    cutoff = time.time() - hours * 3600
    n = 0
    for name in os.listdir(temp_dir):
        p = os.path.join(temp_dir, name)
        try:
            if os.path.getmtime(p) < cutoff:
                recycle(p)
                n += 1
        except Exception:
            pass
    return n
