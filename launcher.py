"""Desktop MCP Bridge launcher: the process Claude desktop starts (stdio MCP). Python stdlib only.

It does no MCP work itself. It (1) starts the elevated worker (ShellExecute 'runas'; scheduled-task fallback), (2) connects to two
user-only named pipes the worker creates, (3) proves the per-launch token, and (4) relays raw MCP bytes: stdin -> pipe, pipe -> stdout.
When Claude closes our stdin, we close the pipes and the worker exits on its own.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import secrets
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SELF_PID = os.getpid()
TASK_NAME = "DesktopBridgeWorker"


def err(msg: str) -> None:
    sys.stderr.write(f"[desktop-bridge launcher] {msg}\n")
    sys.stderr.flush()


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("fMask", wt.ULONG), ("hwnd", wt.HWND), ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR),
                ("lpParameters", wt.LPCWSTR), ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY), ("dwHotKey", wt.DWORD),
                ("hIcon", wt.HANDLE), ("hProcess", wt.HANDLE)]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def start_worker(pipe: str, token: str):
    """Returns a process handle (int) or None. Raises RuntimeError when no way to start the elevated worker works."""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    params = f"-m desktop_bridge.worker --pipe {pipe} --token {token} --launcher-pid {SELF_PID}"
    if is_admin():  # already elevated (e.g. started from an admin shell): no need for runas
        subprocess.Popen([pythonw] + params.split(), cwd=ROOT, creationflags=0x08000000)
        return None
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = pythonw
    sei.lpParameters = params
    sei.lpDirectory = ROOT
    sei.nShow = 0  # SW_HIDE
    if ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        return sei.hProcess
    code = ctypes.get_last_error() or ctypes.windll.kernel32.GetLastError()
    err(f"runas failed (error {code}); trying the scheduled-task fallback '{TASK_NAME}'")
    hp = os.path.join(os.environ.get("LOCALAPPDATA", ROOT), "desktop-bridge")
    os.makedirs(hp, exist_ok=True)
    path = os.path.join(hp, "handoff.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pipe": pipe, "token": token, "pid": SELF_PID}, f)
    r = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True, text=True, creationflags=0x08000000)
    if r.returncode != 0:
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError(f"cannot start the elevated worker: runas error {code}; task '{TASK_NAME}' not runnable "
                           f"({(r.stderr or r.stdout).strip()[:120]}). Run install-elevated-task.ps1 once.")
    return None


def worker_exited(h) -> int | None:
    if not h:
        return None
    if ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0:
        code = wt.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value
    return None


def open_pipe(path: str, mode: str, h, deadline: float):
    last = None
    while time.monotonic() < deadline:
        try:
            return open(path, mode, buffering=0)
        except (FileNotFoundError, PermissionError, OSError) as e:
            last = e
        code = worker_exited(h)
        if code is not None:
            raise RuntimeError(f"worker exited early (code {code}); see %TEMP%\\desktop-bridge\\worker.log")
        time.sleep(0.1)
    raise RuntimeError(f"could not open {path}: {last}")


def write_all(f, data: bytes) -> None:
    mv = memoryview(data)
    while mv:
        n = f.write(mv)
        mv = mv[n:]


def main() -> int:
    pipe = "desktop-bridge-" + secrets.token_hex(16)
    token = secrets.token_hex(24)
    try:
        h = start_worker(pipe, token)
        deadline = time.monotonic() + 40
        pin = open_pipe(r"\\.\pipe" + "\\" + pipe + "-in", "wb", h, deadline)
        pout = open_pipe(r"\\.\pipe" + "\\" + pipe + "-out", "rb", h, deadline)
        write_all(pin, f"HELLO {token}\n".encode("ascii"))
        reply = b""
        while not reply.endswith(b"\n") and len(reply) < 16:
            ch = pout.read(1)
            if not ch:
                raise RuntimeError("worker closed the pipe during the handshake")
            reply += ch
        if reply.strip() != b"OK":
            raise RuntimeError("handshake rejected by worker")
    except Exception as e:
        err(str(e))
        return 1

    def pump_in():
        try:
            while True:
                chunk = sys.stdin.buffer.read1(65536)
                if not chunk:
                    break
                write_all(pin, chunk)
        except Exception:
            pass
        finally:
            try:
                pin.close()
            except Exception:
                pass

    threading.Thread(target=pump_in, daemon=True).start()
    out = sys.stdout.buffer
    try:
        while True:
            data = pout.read(65536)
            if not data:
                break
            out.write(data)
            out.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
