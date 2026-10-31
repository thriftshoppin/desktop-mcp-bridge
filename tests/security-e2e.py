"""Desktop MCP Bridge security end-to-end tests for the guardian's control pipe (audit DB-1 / DB-2), in the style of the Firefox bridge's security-e2e.

  .venv\\Scripts\\python.exe tests\\security-e2e.py            offline part only: a squatter owns the control pipe, the real launcher must refuse it.
                                                             Opens no window, starts no guardian, touches nothing of the user's.
  .venv\\Scripts\\python.exe tests\\security-e2e.py --live     ALSO starts real elevated guardians (each opens its own visible feed window for a few seconds,
                                                             test instance names only, never the user's `main` guardian): squatted start, flood of silent
                                                             connections, forged pid, wrong session token. No mouse, keyboard or other window is touched.
                                                             Run it only when the user has said OK.
Exit code 0 = every check passed."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import psutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import launcher  # noqa: E402
from desktop_bridge import clients, policy  # noqa: E402

TMP = os.path.expandvars(r"%TEMP%\desktop-bridge")
UID = uuid.uuid4().hex[:6]
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def ctl_name(inst: str) -> str:
    return r"\\.\pipe" + "\\" + f"desktop-bridge-ctl-{inst}-{os.environ.get('USERNAME', 'user').lower()}"


class Squatter:
    """A same-user, Medium-integrity program that created the control pipe name first and records every byte it receives."""

    def __init__(self, inst: str):
        import win32pipe
        self.h = win32pipe.CreateNamedPipe(ctl_name(inst), win32pipe.PIPE_ACCESS_DUPLEX, win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT, 255, 4096, 4096, 0, None)
        self.received = b""
        self.connections = 0
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        import win32file
        import win32pipe
        try:
            while not self.stop:
                win32pipe.ConnectNamedPipe(self.h, None)
                self.connections += 1
                try:
                    self.received += win32file.ReadFile(self.h, 4096)[1]
                except Exception:
                    pass
                break
        except Exception:
            pass

    def close(self):
        import ctypes
        import win32api
        self.stop = True
        try:
            ctypes.windll.kernel32.CancelIoEx(ctypes.c_void_p(int(self.h)), None)   # a thread may be blocked in ConnectNamedPipe/ReadFile on this handle: cancel first, or CloseHandle hangs
            win32api.CloseHandle(self.h)
        except Exception:
            pass


def launcher_env(inst: str, **extra) -> dict:
    return {**os.environ, "DESKTOP_BRIDGE_INSTANCE": inst, **extra}


INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "sec-e2e", "version": "1"}}})


def offline() -> None:
    print("OFFLINE: squatter vs the real launcher")
    inst = f"sec-sq-{UID}"
    sq = Squatter(inst)
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, os.path.join(ROOT, "launcher.py")], input=(INIT + "\n").encode(), capture_output=True, cwd=ROOT, env=launcher_env(inst), timeout=60)
    dt = time.monotonic() - t0
    err = p.stderr.decode(errors="replace")
    sq.close()
    record("SEC-1a the launcher refuses a control pipe owned by an impostor (exit 1, clear message)", p.returncode == 1 and "refusing" in err, f"exit={p.returncode} in {dt:.1f}s msg={err.strip()[:110]!r}")
    record("SEC-1b the impostor received 0 bytes and the launcher printed nothing to the client", sq.received == b"" and p.stdout == b"", f"squatter received {len(sq.received)} bytes, stdout {len(p.stdout)} bytes")
    record("SEC-1c no guardian was started because of the impostor (the launcher did not 'help' by starting one)",
           not any(f"instance {inst}" in open(os.path.join(TMP, f), encoding="utf-8", errors="replace").read()
                   for f in os.listdir(TMP) if f.startswith("worker-") and f.endswith(".log") and time.time() - os.path.getmtime(os.path.join(TMP, f)) < 120),
           "no worker log mentions this test instance")


def seeded_store() -> tuple[str, str]:
    """A throwaway state folder in which the current config is approved and THIS script is an allowed client (no questions in any window)."""
    store = tempfile.mkdtemp(prefix="bridge-sec-state-")
    policy.Approval(ROOT, store).approve()
    me = psutil.Process(os.getpid())
    clients.AllowList(store).add(clients.identity_key(me.exe(), os.path.abspath(sys.argv[0])), "sec-e2e", me.exe())
    return store, me.exe()


def worker_log_with(text: str, since: float) -> str:
    for f in sorted((os.path.join(TMP, x) for x in os.listdir(TMP) if x.startswith("worker-") and x.endswith(".log")), key=os.path.getmtime, reverse=True)[:8]:
        if os.path.getmtime(f) >= since - 2:
            body = open(f, encoding="utf-8", errors="replace").read()
            if text in body:
                return f
    return ""


def live() -> None:
    import win32file
    print("LIVE: real elevated test guardians (own feed windows, test instance names)")
    # ---- SEC-1d: a squatter was there first; the guardian must say so loudly and stop instead of running without a control pipe
    inst = f"sec-gs-{UID}"
    store, _ = seeded_store()
    sq = Squatter(inst)
    since = time.time()
    sei = launcher.SHELLEXECUTEINFOW()
    sei.cbSize = launcher.ctypes.sizeof(sei)
    sei.fMask = 0x40
    sei.lpVerb = "runas"
    sei.lpFile = sys.executable
    sei.lpParameters = subprocess.list2cmdline(["-m", "desktop_bridge.worker", "--guardian", "--instance", inst, "--approval-dir", store])
    sei.lpDirectory = ROOT
    sei.nShow = 4
    ok = bool(launcher.ctypes.windll.shell32.ShellExecuteExW(launcher.ctypes.byref(sei)))
    gpid = None
    for _ in range(40):
        time.sleep(0.5)
        f = worker_log_with("CONTROL PIPE UNAVAILABLE", since)
        if f:
            gpid = int(os.path.basename(f)[7:-4])
            break
    time.sleep(1.5)
    gone = gpid is not None and not psutil.pid_exists(gpid)
    record("SEC-1d a guardian that cannot create the control pipe (squatted) says so in its feed/log and exits (no silent dead thread)", ok and bool(gpid) and gone,
           f"started={ok} message found={bool(gpid)} guardian pid={gpid} exited={gone}")
    sq.close()

    # ---- a real guardian for the flood / forged-pid / wrong-token checks
    inst = f"sec-fl-{UID}"
    store, _ = seeded_store()
    env = launcher_env(inst, DESKTOP_BRIDGE_APPROVAL_DIR=store, DESKTOP_BRIDGE_IDLE_EXIT_S="10")
    a = subprocess.Popen([sys.executable, os.path.join(ROOT, "launcher.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=env)
    a.stdin.write((INIT + "\n").encode())
    a.stdin.flush()
    first = a.stdout.readline()
    record("SEC-0 a legitimate launcher is still served by a real guardian (the new authentication does not lock out the real thing)", b'"result"' in first, f"initialize answered={b'result' in first}")
    path = ctl_name(inst)

    # ---- SEC-2: flood of silent connections (DB-2)
    t0 = time.monotonic()
    conns = []
    import win32pipe
    for _ in range(80):
        for _try in range(40):   # the guardian offers one free instance at a time: wait (up to 2 s) for the next one when it is momentarily busy
            try:
                conns.append(win32file.CreateFile(path, 0xC0000000, 0, None, 3, 0, None))
                break
            except Exception:
                try:
                    win32pipe.WaitNamedPipe(path, 50)
                except Exception:
                    time.sleep(0.05)
    opened = len(conns)
    closed_fast = 0
    time.sleep(0.5)
    for c in conns:   # the per-process cap closes the extras at once: a read returns immediately with an error / EOF
        res = {}

        def rd(c=c, res=res):
            try:
                res["v"] = win32file.ReadFile(c, 16)[1]
            except Exception as e:
                res["e"] = e
        th = threading.Thread(target=rd, daemon=True)
        th.start()
        th.join(0.15)
        if not th.is_alive() and "e" in res:
            closed_fast += 1
    # while the flood is still held open, a second legitimate launcher must get through
    b = subprocess.Popen([sys.executable, os.path.join(ROOT, "launcher.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=env)
    b.stdin.write((INIT + "\n").encode())
    b.stdin.flush()
    out: dict = {}
    th = threading.Thread(target=lambda: out.setdefault("l", b.stdout.readline()), daemon=True)
    th.start()
    th.join(15)
    served = b'"result"' in out.get("l", b"")
    record("SEC-2a while 80 silent connections are held open a legitimate launcher is still served", served, f"flood connections opened={opened}, launcher served={served} after {time.monotonic() - t0:.1f}s")
    record("SEC-2b the per-process cap closes the excess connections at once (at most 4 of the 80 stay open)", opened >= 60 and opened - closed_fast <= 4 + 1, f"closed immediately={closed_fast} of {opened}, still open={opened - closed_fast}")
    time.sleep(6)   # past the 5 s read timeout
    still = 0
    for c in conns:
        res = {}

        def rd2(c=c, res=res):
            try:
                res["v"] = win32file.ReadFile(c, 16)[1]
            except Exception as e:
                res["e"] = e
        th = threading.Thread(target=rd2, daemon=True)
        th.start()
        th.join(0.15)
        if th.is_alive():
            still += 1
    record("SEC-2c silent connections are dropped after the 5 s read timeout (none still waiting 6.5 s later)", still == 0, f"still open after timeout={still}")
    for c in conns:
        try:
            win32file.CloseHandle(c)
        except Exception:
            pass

    # ---- SEC-3: forged pid on the control pipe
    c = win32file.CreateFile(path, 0xC0000000, 0, None, 3, 0, None)
    win32file.WriteFile(c, (json.dumps({"op": "connect", "pid": 4}) + "\n").encode())
    reply = json.loads(win32file.ReadFile(c, 4096)[1].decode() or "{}")
    win32file.CloseHandle(c)
    record("SEC-3 a connect request that claims another process's pid is refused (the pid must equal the kernel-reported pipe client)", reply.get("ok") is False and "bad request" in reply.get("reason", ""), f"reply={reply}")

    # ---- SEC-4: right to ask for a session (this script is allowed) but a wrong session token
    c = win32file.CreateFile(path, 0xC0000000, 0, None, 3, 0, None)
    win32file.WriteFile(c, (json.dumps({"op": "connect", "pid": os.getpid()}) + "\n").encode())
    sess = json.loads(win32file.ReadFile(c, 4096)[1].decode() or "{}")
    win32file.CloseHandle(c)
    got_ok = False
    if sess.get("ok"):
        pin = launcher.open_pipe(r"\\.\pipe" + "\\" + sess["pipe"] + "-in", "wb", time.monotonic() + 10)
        pout = launcher.open_pipe(r"\\.\pipe" + "\\" + sess["pipe"] + "-out", "rb", time.monotonic() + 10)
        pin.write(b"HELLO " + b"0" * 48 + b"\n")
        box: dict = {}

        def rd_ok():
            try:
                box["v"] = pout.read(3)
            except Exception:
                box["v"] = b""
        th = threading.Thread(target=rd_ok, daemon=True)
        th.start()
        th.join(8)   # no 'OK' within 8 s (or an immediate close) = not served
        got_ok = box.get("v", b"").strip() == b"OK"
        for f in ((pin,) if th.is_alive() else (pin, pout)):   # never close a handle another thread is still blocked reading: that hangs
            try:
                f.close()
            except Exception:
                pass
    record("SEC-4 a session pipe with a WRONG token is not served (no 'OK')", bool(sess.get("ok")) and not got_ok, f"session granted to this allowed client={bool(sess.get('ok'))}, wrong token answered OK={got_ok}")
    b2 = subprocess.Popen([sys.executable, os.path.join(ROOT, "launcher.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=env)
    b2.stdin.write((INIT + "\n").encode())
    b2.stdin.flush()
    out2: dict = {}
    th = threading.Thread(target=lambda: out2.setdefault("l", b2.stdout.readline()), daemon=True)
    th.start()
    th.join(15)
    record("SEC-5 after all of the above the guardian still serves a legitimate launcher", b'"result"' in out2.get("l", b""), f"served={b'result' in out2.get('l', b'')}")
    for pr in (a, b, b2):
        subprocess.run(["taskkill", "/PID", str(pr.pid), "/F", "/T"], capture_output=True)


def main() -> int:
    t0 = time.time()
    offline()
    if "--live" in sys.argv:
        live()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed in {time.time() - t0:.0f}s" + ("" if not bad else "  FAILED: " + "; ".join(r[0][:40] for r in bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
