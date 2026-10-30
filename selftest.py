"""Desktop MCP Bridge self-test. Drives the server through a real MCP stdio client, via the LAUNCHER (so the elevated path is exercised).
EVERYTHING HAPPENS IN VISIBLE WINDOWS and takes over the mouse/keyboard for several minutes: run it only when the user is at the PC and has said OK.
Usage: .venv\\Scripts\\python.exe selftest.py [all|core|control|sessions]
It needs no input from the user. Esc / focus / minimize 'user' actions come from a separate ELEVATED helper process (tests/helper_elev.py), not from the bridge.
The physical-keypress sign-off (a real Esc on the real keyboard) is NOT part of this script."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import anyio
import psutil
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import launcher  # noqa: E402  (ShellExecute structs for the elevated helper)
from desktop_bridge import policy  # noqa: E402

LOG = r"C:\Users\SysAdmin\Claude\claude-actions-log.md"
LOGDIR = r"C:\Users\SysAdmin\Claude\log-tools"
TMP = os.path.expandvars(r"%TEMP%\desktop-bridge")
HELPER = os.path.join(ROOT, "tests", "helper_elev.py")
FEED = "Desktop Bridge - live action feed"
RESULTS: list[tuple[str, bool, str]] = []
CALLS: dict[str, int] = {}
UID = uuid.uuid4().hex[:8]
u32 = ctypes.WinDLL("user32", use_last_error=True)


def record(name: str, ok, detail: str = "") -> None:
    """ok: True = PASS, False = FAIL, None = SKIP (not run on purpose; counted separately)."""
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'SKIP' if ok is None else 'FAIL'}  {name}  {detail}", flush=True)


class Client:
    def __init__(self, s: ClientSession):
        self.s = s

    async def call(self, name: str, **args):
        args = {k: v for k, v in args.items() if v is not None}
        CALLS[name] = CALLS.get(name, 0) + 1
        res = await self.s.call_tool(name, args)
        txt = next((c.text for c in res.content if getattr(c, "type", "") == "text"), "{}")
        data = json.loads(txt)
        data["_images"] = sum(1 for c in res.content if getattr(c, "type", "") == "image")
        img = next((c for c in res.content if getattr(c, "type", "") == "image"), None)
        if img is not None:
            data["_image"] = base64.b64decode(img.data)
        data["_is_error"] = res.is_error
        return data


def err_code(d: dict) -> str:
    return (d.get("error") or {}).get("code", "")


# ----------------------------------------------------------------------------- environment helpers

def make_store() -> str:
    """A temp config-approval store seeded exactly as the user's 'y' would, but only if config.json equals the committed version."""
    head = subprocess.run(["git", "show", "HEAD:config.json"], cwd=ROOT, capture_output=True, text=True).stdout
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cur = json.load(f)
    if json.loads(head) != cur or os.path.exists(os.path.join(ROOT, "config.local.json")):
        print("config.json differs from the committed version (or config.local.json exists): commit/approve it first. Aborting.")
        sys.exit(2)
    d = tempfile.mkdtemp(prefix="bridge-approval-")
    policy.Approval(ROOT, d).approve()
    return d


@contextlib.asynccontextmanager
async def open_session(env: dict | None = None):
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT, "launcher.py")], cwd=ROOT, env=env or {})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield Client(s)


def overlay_windows() -> list[dict]:
    out = []
    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, _):
        buf = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(h, buf, 64)
        if buf.value == "DesktopBridgeOverlay":
            r = wt.RECT()
            u32.GetWindowRect(h, ctypes.byref(r))
            out.append({"hwnd": int(h), "visible": bool(u32.IsWindowVisible(h)), "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
                        "ex": u32.GetWindowLongW(h, -20) & 0xFFFFFFFF})
        return True

    u32.EnumWindows(PROC(cb), 0)
    return out


def visible_overlay() -> list[dict]:
    return [w for w in overlay_windows() if w["visible"]]


def cursor() -> tuple[int, int]:
    p = wt.POINT()
    u32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def feed_log_text() -> str:
    files = [os.path.join(TMP, f) for f in os.listdir(TMP) if f.startswith("worker-") and f.endswith(".log")]
    newest = max(files, key=os.path.getmtime)
    return open(newest, encoding="utf-8", errors="replace").read()


def tail_log(since: str) -> list[str]:
    with open(LOG, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return [ln for ln in lines if ln.startswith("- ") and ln[2:21] >= since]


def run_helper(action: str, at: float, out: str, **kw) -> None:
    """Start the ELEVATED helper (visible console, silent runas)."""
    plist = [HELPER, action, "--at", str(at), "--out", out] + [x for k, v in kw.items() for x in (f"--{k}", str(v))]
    sei = launcher.SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x40
    sei.lpVerb = "runas"
    sei.lpFile = sys.executable
    sei.lpParameters = subprocess.list2cmdline(plist)
    sei.lpDirectory = ROOT
    sei.nShow = 4  # SW_SHOWNOACTIVATE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        raise RuntimeError("could not start the elevated helper")


async def helper_result(out: str, timeout: float = 25.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(out):
            try:
                return json.loads(open(out).read())
            except Exception:
                pass
        await asyncio.sleep(0.1)
    raise RuntimeError("helper produced no result")


async def sleep_until(t: float) -> None:
    while time.time() < t:
        await asyncio.sleep(0.002)


async def wait_window(c: Client, needle: str, timeout: float = 10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        d = await c.call("desktop_windows_list")
        for w in d.get("windows", []):
            if needle.lower() in w["title"].lower():
                return w
        await asyncio.sleep(0.4)
    return None


async def kill_pid(c: Client, pid: int | None):
    if pid:
        await c.call("desktop_run", command=f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue")


async def launch_form(c: Client, mode: str, x: int, y: int, auto_close: int = 150):
    script = os.path.join(ROOT, "tests", "forms.ps1")
    d = await c.call("desktop_launch", app="powershell.exe", wait_ms=500,
                     args=["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Mode", mode, "-X", str(x), "-Y", str(y), "-AutoCloseSec", str(auto_close)])
    w = await wait_window(c, f"BridgeTest {mode}", 15)
    return d.get("pid"), w


# ============================================================================= CORE (the 19 original checks, adapted)

async def t_startup(c: Client, tools: list[str]):
    expected = {"desktop_session_status", "desktop_monitors_list", "desktop_screenshot", "desktop_zoom", "desktop_windows_list",
                "desktop_window_focus", "desktop_ui_tree", "desktop_find", "desktop_click", "desktop_type", "desktop_key",
                "desktop_wait", "desktop_launch", "desktop_clipboard_get", "desktop_clipboard_set", "desktop_run", "desktop_fs",
                "desktop_audio_level", "desktop_resume"}
    record("T01 tool list", expected.issubset(set(tools)), f"{len(tools)} tools, missing={sorted(expected - set(tools))}")
    s = await c.call("desktop_session_status")
    w = s.get("worker", {})
    record("T02 worker is elevated (via launcher, silent runas)", bool(w.get("elevated")) and w.get("integrity") == "high",
           f"elevated={w.get('elevated')} integrity={w.get('integrity')} type={s.get('type')} locked={s.get('locked')} approved={s.get('config_approved')} hotkeys={s.get('kill_switch', {}).get('hotkeys')}")
    win = await wait_window(c, FEED, 5)
    record("T03 worker window is VISIBLE (live action feed)", win is not None and win["state"] != "minimized", f"{'found hwnd ' + str(win['hwnd']) + ' state=' + win['state'] if win else 'not found'}")


def t_unit():
    r = subprocess.run([sys.executable, "-W", "ignore", "-m", "unittest", "discover", "tests"], cwd=ROOT, capture_output=True, text=True)
    ran = [ln for ln in r.stderr.splitlines() if ln.startswith("Ran ")]
    record("T04 unit tests (coords, denylist, keys, paths, policy gates, injection scanner, approval, lockout, control window, no-relax-toggle grep)", r.returncode == 0,
           f"{ran[0] if ran else ''} {(r.stderr.strip().splitlines() or [''])[-1]}")


async def t_coords(c: Client):
    mons = (await c.call("desktop_monitors_list")).get("monitors", [])
    for m in mons:
        pid, w = await launch_form(c, "button", m["rect"][0] + 150, m["rect"][1] + 150)
        name = f"T05 click by ref AND by screenshot coords, monitor {m['id']} ({m['rect'][2]}x{m['rect'][3]} @{m['scale_pct']}%)"
        try:
            if not w:
                record(name, False, "test window did not appear")
                continue
            f = await c.call("desktop_find", query="TargetButton", window=w["hwnd"], role="Button")
            if not f.get("matches"):
                record(name, False, "button not found via UIA")
                continue
            node = f["matches"][0]
            x, y, bw, bh = node["rect"]
            cx, cy = x + bw // 2, y + bh // 2
            on_mon = m["rect"][0] <= cx < m["rect"][0] + m["rect"][2] and m["rect"][1] <= cy < m["rect"][1] + m["rect"][3]
            await c.call("desktop_click", ref=node["ref"])
            await asyncio.sleep(0.4)
            w1 = await wait_window(c, "clicks=1", 3)
            shot = await c.call("desktop_screenshot", monitor=m["id"])
            sc, (ox, oy) = shot["scale"], shot["origin"]
            sx, sy = (cx - ox) * sc, (cy - oy) * sc
            d2 = await c.call("desktop_click", x=sx, y=sy, coords="shot")
            await asyncio.sleep(0.4)
            w2 = await wait_window(c, "clicks=2", 3)
            drift = max(abs(d2["clicked"][0] - cx), abs(d2["clicked"][1] - cy)) if d2.get("clicked") else 99
            ok = bool(on_mon and w1 and w2 and drift <= int(1 / sc) + 1)
            record(name, ok, f"window on monitor={on_mon} ref-click={'ok' if w1 else 'MISS'} shot-click={'ok' if w2 else 'MISS'} drift={drift}px scale={sc:.4f} engine={shot.get('engine')} glide={d2.get('glide_ms')} ms")
        finally:
            await kill_pid(c, pid)
            await asyncio.sleep(0.5)


async def t_notepad(c: Client):
    name = "T06 Notepad round trip (launch, visible click+typing, ui_tree shows text, ctrl+s, fs read matches, close tab)"
    if any((p.info["name"] or "").lower() == "notepad.exe" for p in psutil.process_iter(["name"])):
        record(name, None, "SKIPPED: Notepad is already running (the user's own tabs would be at risk). Close Notepad and run `selftest.py core` to include this check.")
        return
    os.makedirs(TMP, exist_ok=True)
    path = os.path.join(TMP, f"selftest-notepad-{UID}.txt")
    text = "bridge selftest \u00fcn\u00ef \u2713 123"
    await c.call("desktop_fs", op="write", path=path, content="")
    await c.call("desktop_launch", app="notepad.exe", args=[path], wait_ms=2500)
    w = await wait_window(c, f"selftest-notepad-{UID}", 12)
    if not w:
        record(name, False, "Notepad window with our file did not appear")
        return
    await c.call("desktop_window_focus", hwnd=w["hwnd"])
    now = await wait_window(c, f"selftest-notepad-{UID}", 3)
    if not now or f"selftest-notepad-{UID}" not in now["title"] or not now.get("focused"):
        record(name, False, f"our tab is not the active, focused one (title={now['title'] if now else None!r}); NOT typing, to protect the user's tabs")
        return
    f = await c.call("desktop_find", query="", window=w["hwnd"], role="Document", limit=3)
    if not f.get("matches"):
        f = await c.call("desktop_find", query="", window=w["hwnd"], role="Edit", limit=3)
    if not f.get("matches"):
        record(name, False, "no Document/Edit element")
        return
    t = await c.call("desktop_type", text=text, ref=f["matches"][0]["ref"])
    await asyncio.sleep(0.5)
    tree = await c.call("desktop_ui_tree", window=w["hwnd"], filter="selftest \u00fcn", depth=8)
    shows = "bridge selftest" in json.dumps(tree, ensure_ascii=False)
    await c.call("desktop_key", combo="ctrl+s")
    got = None
    for _ in range(25):
        await asyncio.sleep(0.3)
        r = await c.call("desktop_fs", op="read", path=path)
        if r.get("content", "").lstrip("\ufeff") == text:
            got = r["content"]
            break
    await c.call("desktop_key", combo="ctrl+w")
    await asyncio.sleep(0.6)
    gone = await wait_window(c, f"selftest-notepad-{UID}", 1.5)
    record(name, bool(shows and got is not None and t.get("method") == "keystrokes"), f"typed via {t.get('method')} ({t.get('ms_per_char')} ms/char); ui_tree shows text={shows}; file exact match={got is not None}; tab closed={gone is None}")
    await c.call("desktop_fs", op="delete", path=path)


async def t_elevated_input(c: Client):
    name = "T07 input into an ELEVATED window (Task Manager)"
    pre = await c.call("desktop_windows_list")
    if any(w["process"].lower() == "taskmgr.exe" for w in pre.get("windows", [])):
        record(name, None, "SKIPPED: Task Manager was already open (not touching the user's window)")
        return
    await c.call("desktop_launch", app="taskmgr.exe", wait_ms=4000)
    w = await wait_window(c, "Task Manager", 10)
    if not w:
        record(name, False, "Task Manager window did not appear")
        return
    await asyncio.sleep(1.5)
    await c.call("desktop_window_focus", hwnd=w["hwnd"])
    f = await c.call("desktop_find", query="search", window=w["hwnd"], role="Edit", limit=3)
    if not f.get("matches"):
        f = await c.call("desktop_find", query="Type a name", window=w["hwnd"], limit=3)
    if not f.get("matches"):
        await c.call("desktop_key", combo="alt+f4")
        record(name, False, "search box not found")
        return
    c1 = await c.call("desktop_click", ref=f["matches"][0]["ref"])
    t = await c.call("desktop_type", text="z")
    await asyncio.sleep(0.5)
    f2 = await c.call("desktop_find", query="search", window=w["hwnd"], role="Edit", limit=3)
    val = (f2.get("matches") or [{}])[0].get("value", "")
    await c.call("desktop_key", combo="ctrl+a")
    await c.call("desktop_key", combo="backspace")
    await asyncio.sleep(0.3)
    f3 = await c.call("desktop_find", query="search", window=w["hwnd"], role="Edit", limit=3)
    cleared = not (f3.get("matches") or [{}])[0].get("value", "")
    await c.call("desktop_window_focus", hwnd=w["hwnd"])
    await c.call("desktop_key", combo="alt+f4")
    await asyncio.sleep(1.0)
    still = await wait_window(c, "Task Manager", 1.5)
    record(name, "z" in val, f"search value after typing={val!r}; cleared={cleared}; closed={still is None}; click={c1.get('ok')} type={t.get('ok')}")


async def t_clipboard(c: Client):
    saved = await c.call("desktop_clipboard_get")
    sample = "h\u00e9llo \u2713 \u65e5\u672c\u8a9e \U0001F600 caf\u00e9"
    await c.call("desktop_clipboard_set", text=sample)
    got = await c.call("desktop_clipboard_get")
    await c.call("desktop_clipboard_set", text=saved.get("text", ""))
    record("T08 clipboard round trip (unicode)", got.get("text") == sample and got.get("untrusted") is True, f"round trip {'exact' if got.get('text') == sample else 'MISMATCH'}; result labelled untrusted={got.get('untrusted')}; user's clipboard restored")


async def t_password(c: Client):
    name = "T09 password field refused, log says [redacted]"
    secret = f"S3cr3t-{UID}"
    pid, w = await launch_form(c, "password", 300, 300)
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if not w:
            record(name, False, "form did not appear")
            return
        pwf = await c.call("desktop_find", query="PwBox", window=w["hwnd"], role="Edit")
        plf = await c.call("desktop_find", query="PlainBox", window=w["hwnd"], role="Edit")
        if not pwf.get("matches") or not plf.get("matches"):
            record(name, False, "text boxes not found")
            return
        pwref, plref = pwf["matches"][0]["ref"], plf["matches"][0]["ref"]
        r1 = await c.call("desktop_type", text=secret, ref=pwref)
        await c.call("desktop_click", ref=pwref)
        r2 = await c.call("desktop_type", text=secret)
        r3 = await c.call("desktop_type", text="plain-ok", ref=plref)
        await asyncio.sleep(3.0)
        lines = tail_log(since)
        mine = [ln for ln in lines if "desktop_type" in ln]
        leaked = any(secret in ln for ln in lines)
        redacted = any("[redacted]" in ln and "PASSWORD_FIELD" in ln for ln in mine)
        ok = err_code(r1) == "PASSWORD_FIELD" and err_code(r2) == "PASSWORD_FIELD" and r3.get("ok") and redacted and not leaked
        record(name, bool(ok), f"by ref -> {err_code(r1)}; by focus -> {err_code(r2)}; plain box ok={r3.get('ok')}; log has [redacted]+PASSWORD_FIELD={redacted}; secret in log={leaked}")
    finally:
        await kill_pid(c, pid)


async def t_kill_combos(c: Client):
    for combo in ("ctrl+alt+pause", "ctrl+alt+shift+k"):
        name = f"T10 kill switch {combo}: halts a running wait, refuses next call until desktop_resume"
        task = asyncio.create_task(c.call("desktop_wait", seconds=30))
        await asyncio.sleep(1.0)
        t0 = time.perf_counter()
        # injected from the ELEVATED worker itself: a Medium process cannot trigger a hotkey owned by a High-integrity one (UIPI)
        await c.call("desktop_key", combo=combo)
        try:
            res = await asyncio.wait_for(task, 6)
        except asyncio.TimeoutError:
            record(name, False, "wait did not halt within 6 s")
            continue
        halt_ms = (time.perf_counter() - t0) * 1000
        nxt = await c.call("desktop_monitors_list")
        st = await c.call("desktop_session_status")
        res_ = await c.call("desktop_resume")
        after = await c.call("desktop_monitors_list")
        ok = err_code(res) == "KILL_SWITCH" and err_code(nxt) == "KILL_SWITCH" and st["kill_switch"]["active"] and res_.get("was_active") and after.get("ok")
        record(name, bool(ok), f"halted in {halt_ms:.0f} ms ({err_code(res)}); next call -> {err_code(nxt)}; source={st['kill_switch']['source']!r}; after resume ok={after.get('ok')}")


async def t_fs(c: Client):
    name = "T11 fs: write/read/copy/move, delete -> Recycle Bin, protected paths refused (log, log tools, config.json)"
    os.makedirs(TMP, exist_ok=True)
    base = os.path.join(TMP, f"bridge-selftest-{UID}")
    p1, p2, p3 = base + ".txt", base + "-copy.txt", base + "-moved.txt"
    w = await c.call("desktop_fs", op="write", path=p1, content="line one\n\u00fcn\u00ef\n")
    r = await c.call("desktop_fs", op="read", path=p1)
    cp = await c.call("desktop_fs", op="copy", path=p1, dest=p2)
    mv = await c.call("desktop_fs", op="move", path=p2, dest=p3)
    ls = await c.call("desktop_fs", op="list", path=TMP)
    await c.call("desktop_fs", op="delete", path=p1)
    await c.call("desktop_fs", op="delete", path=p3)
    gone = not os.path.exists(p1) and not os.path.exists(p3)
    rb = await c.call("desktop_run", command="(New-Object -ComObject Shell.Application).NameSpace(10).Items() | Where-Object { $_.Name -like 'bridge-selftest-" + UID + "*' } | ForEach-Object { $_.Name }")
    in_bin = len([ln for ln in rb.get("stdout", "").splitlines() if ln.strip()])
    prot = await c.call("desktop_fs", op="write", path=LOG, content="x", append=True)
    prot2 = await c.call("desktop_fs", op="delete", path=os.path.join(LOGDIR, "Add-ClaudeLog.ps1"))
    cfgw = await c.call("desktop_fs", op="write", path=os.path.join(ROOT, "config.json"), content="{}")
    cfgr = await c.call("desktop_run", command=f"Set-Content '{os.path.join(ROOT, 'config.json')}' '{{}}'")
    ok = (r.get("content", "").startswith("line one") and cp.get("ok") and mv.get("ok") and w.get("ok") and gone and in_bin >= 2
          and err_code(prot) == "PROTECTED_PATH" and err_code(prot2) == "PROTECTED_PATH" and err_code(cfgw) == "PROTECTED_PATH" and err_code(cfgr) == "PROTECTED_PATH"
          and any(e["name"].startswith(os.path.basename(base)) for e in ls.get("entries", [])))
    record(name, bool(ok), f"gone from disk={gone}; in Recycle Bin={in_bin}/2; log write -> {err_code(prot)}; log-tool delete -> {err_code(prot2)}; config.json via fs -> {err_code(cfgw)}; via run -> {err_code(cfgr)}")


async def t_run_ui(c: Client):
    pid, w = await launch_form(c, "button", 1200, 500)
    r = await c.call("desktop_run", command="Write-Output 'h\u00e9llo \u2713'; $PSVersionTable.PSVersion.Major; exit 3")
    z = await c.call("desktop_zoom", region=[0, 0, 300, 200], coords="screen")
    wl = await c.call("desktop_windows_list")
    tree = await c.call("desktop_ui_tree", window=w["hwnd"] if w else None, depth=4, max_nodes=300)
    timeout_r = await c.call("desktop_run", command="Start-Sleep -Seconds 20", timeout=2)
    await kill_pid(c, pid)
    record("T12 run/zoom/windows_list/ui_tree basics (+untrusted labels)",
           r.get("exit_code") == 3 and "h\u00e9llo \u2713" in r.get("stdout", "") and z.get("_images") == 1 and wl.get("count", 0) > 0 and tree.get("nodes", 0) > 0
           and timeout_r.get("timed_out") and tree.get("untrusted") and r.get("untrusted"),
           f"run exit={r.get('exit_code')}; zoom image={z.get('_images')} engine={z.get('engine')}; windows={wl.get('count')} (non_target={sum(1 for x in wl.get('windows', []) if x.get('non_target'))}); "
           f"ui_tree nodes={tree.get('nodes')} in {tree.get('elapsed_ms')} ms; timeout kill={timeout_r.get('timed_out')}; untrusted label on tree/run={tree.get('untrusted')}/{r.get('untrusted')}")


async def t_audio(c: Client):
    a = await c.call("desktop_audio_level")
    shot = await c.call("desktop_screenshot")
    record("T13 audio_level picks the device with signal (+ capture still works with both libs loaded)", a.get("ok") and shot.get("ok") and shot.get("engine", "").startswith("dxcam"),
           f"device={a.get('device')!r} peak={a.get('peak')} playing={a.get('playing')} default={a.get('default_device')!r}; screenshot engine={shot.get('engine')}")


async def t_perf(c: Client):
    ts = []
    d = {}
    for _ in range(5):
        t0 = time.perf_counter()
        d = await c.call("desktop_screenshot")
        ts.append((time.perf_counter() - t0) * 1000)
    tv = await c.call("desktop_screenshot", monitor=1)
    record("T14 screenshot timing", True, f"1080p x5: median {sorted(ts)[2]:.0f} ms (engine {d.get('engine')}); 4K monitor: engine {tv.get('engine')} {tv.get('image_size')}")


# ============================================================================= M0.3: control window, border, mouse, Esc, self-protection

async def t_border(c: Client):
    mons = (await c.call("desktop_monitors_list")).get("monitors", [])
    s = {}
    for _ in range(40):
        s = await c.call("desktop_session_status")
        if not s["in_control"]:
            break
        await asyncio.sleep(0.5)
    idle_hidden = not visible_overlay()
    record("T20 border hidden while idle (outside the control window)", idle_hidden and not s["in_control"], f"in_control={s['in_control']} visible overlay windows={len(visible_overlay())} (grace {s.get('idle_grace_s')} s)")
    shot = await c.call("desktop_screenshot")
    s = await c.call("desktop_session_status")
    vis = visible_overlay()
    per = {}
    for m in mons:
        mx, my, mw, mh = m["rect"]
        t = round(6 * m["dpi"] / 96)
        top = [w for w in vis if w["rect"][:2] == (mx, my) and w["rect"][2] == mw and w["rect"][3] == t]
        pill = [w for w in vis if mx < w["rect"][0] < mx + mw and w["rect"][2] > 200 and w["rect"][1] < my + 3 * t + 20 and w["rect"][3] < 80]
        per[m["id"]] = (len(top), len(pill), t)
    flags = all((w["ex"] & 0x08000000) and (w["ex"] & 0x20) and (w["ex"] & 0x8) and (w["ex"] & 0x80) and (w["ex"] & 0x80000) for w in vis)
    ok = all(v[0] == 1 and v[1] >= 1 for v in per.values()) and s["border_lead_ms_last"] >= 148 and flags
    record("T21 border appears on BOTH monitors before the first action (>=150 ms lead), correct DPI-scaled thickness, click-through/no-activate/topmost styles",
           bool(ok), f"per monitor (top-bar windows, pill windows, expected px)={per}; lead before action={s['border_lead_ms_last']} ms; styles ok={flags}; overlay.ok={s['overlay']['ok']}")
    img = Image.open(io.BytesIO(shot["_image"])).convert("RGB")
    w_, h_ = img.size
    px = [img.getpixel((w_ // 2, y)) for y in range(0, 6)] + [img.getpixel((x, h_ // 2)) for x in range(0, 6)]
    orange = [p for p in px if abs(p[0] - 255) < 30 and abs(p[1] - 140) < 40 and p[2] < 60]
    record("T22 border is NOT in the bridge's own screenshots (WDA_EXCLUDEFROMCAPTURE) while it is visible", len(vis) > 0 and not orange and s["overlay"]["excluded_from_capture"],
           f"visible overlay windows={len(vis)}; orange pixels in edge samples={len(orange)}/12; excluded_from_capture={s['overlay']['excluded_from_capture']} (OBS display capture: not tested)")
    t_last = time.time()
    gone_at = None
    for _ in range(300):
        await asyncio.sleep(0.1)
        if not visible_overlay():
            gone_at = time.time() - t_last
            break
    grace = s["idle_grace_s"]
    record("T23 border disappears after idle_grace_s", gone_at is not None and grace - 0.5 <= gone_at <= grace + 2.0, f"hidden {gone_at if gone_at is None else round(gone_at, 2)} s after the last call (grace {grace} s)")


async def t_clickthrough(c: Client):
    m = (await c.call("desktop_monitors_list"))["monitors"][0]
    pid, w = await launch_form(c, "edge", m["rect"][0], m["rect"][1])
    try:
        if not w:
            record("T24 click lands THROUGH the border", False, "edge form did not appear")
            return
        await c.call("desktop_screenshot")
        await asyncio.sleep(0.4)
        vis = visible_overlay()
        r = await c.call("desktop_click", x=m["rect"][0] + 3, y=m["rect"][1] + 3, coords="screen")
        await asyncio.sleep(0.5)
        hit = await wait_window(c, "BridgeTest edge clicks=1", 3)
        record("T24 a real click at the very corner lands THROUGH the border on the window underneath", bool(hit and vis and r.get("ok")), f"overlay visible={len(vis)} windows; click ok={r.get('ok')}; window under it counted the click={bool(hit)}")
    finally:
        await kill_pid(c, pid)


async def t_mouse(c: Client):
    pid, w = await launch_form(c, "button", 1200, 450)
    try:
        f = await c.call("desktop_find", query="TargetButton", window=w["hwnd"], role="Button")
        node = f["matches"][0]
        x, y, bw, bh = node["rect"]
        cx, cy = x + bw // 2, y + bh // 2
        ctypes.windll.user32.SetCursorPos(-3000, -900)
        samples = []
        halo = []
        stop = threading.Event()

        def sampler():
            while not stop.is_set():
                samples.append((time.perf_counter(), *cursor()))
                halo.append(any(40 < v["rect"][2] <= 300 and v["rect"][3] <= 300 for v in visible_overlay()))
                time.sleep(0.004)

        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        r = await c.call("desktop_click", ref=node["ref"])
        await asyncio.sleep(0.45)
        stop.set()
        th.join()
        pos = [(s[1], s[2]) for s in samples]
        moved = [s for s in samples if (s[1], s[2]) != pos[0]]
        distinct = len(set(pos))
        dur = (moved[-1][0] - moved[0][0]) * 1000 if moved else 0
        final = cursor()
        near = abs(final[0] - cx) <= 1 and abs(final[1] - cy) <= 1
        hit = await wait_window(c, "clicks=1", 2)
        record("T25 mouse: the REAL cursor glides (not a teleport) to the target, a halo appears, then a real click", bool(near and distinct >= 6 and dur >= 100 and any(halo) and hit),
               f"{distinct} distinct cursor positions over {dur:.0f} ms (tool says glide {r.get('glide_ms')} ms); final {final} vs target ({cx},{cy}); halo window seen={any(halo)}; click counted={bool(hit)}")
    finally:
        await kill_pid(c, pid)


async def t_typing(c: Client):
    pid, w = await launch_form(c, "password", 300, 450)
    try:
        f = await c.call("desktop_find", query="PlainBox", window=w["hwnd"], role="Edit")
        ref = f["matches"][0]["ref"]
        text = "visible typing: 0123456789 abcdefghijklmnop"
        r = await c.call("desktop_type", text=text, ref=ref)
        await asyncio.sleep(0.3)
        v = (await c.call("desktop_find", query="PlainBox", window=w["hwnd"], role="Edit"))["matches"][0].get("value", "")
        short = await c.call("desktop_type", text="short", ref=ref, method="value")
        longr = await c.call("desktop_type", text="L" * 250, ref=ref, method="value")
        announced = "type method=value" in feed_log_text()
        ok = (r.get("method") == "keystrokes" and 8 <= (r.get("ms_per_char") or 0) <= 60 and v.startswith("visible typing") and err_code(short) == "VALUE_METHOD_TOO_SHORT"
              and longr.get("ok") and announced)
        record("T26 typing is visible by default (real keystrokes ~8 ms/char); method=value refused when short, allowed + announced in the feed when long", bool(ok),
               f"default: method={r.get('method')} {r.get('ms_per_char')} ms/char, text landed={v.startswith('visible typing')}; value+short -> {err_code(short)}; value+250 chars ok={longr.get('ok')}; announced in feed={announced}")
    finally:
        await kill_pid(c, pid)


async def t_esc(c: Client):
    out = os.path.join(TMP, f"helper-{UID}")
    pid, w = await launch_form(c, "button", 1250, 500)
    await c.call("desktop_screenshot")
    at = time.time() + 3.5
    run_helper("esc", at, out + "1.json")
    task = asyncio.create_task(c.call("desktop_wait", seconds=30))
    res = await asyncio.wait_for(task, 25)
    t_ret = time.time()
    info = await helper_result(out + "1.json")
    halt = (t_ret - info["t_down"][0]) * 1000
    st = await c.call("desktop_session_status")
    nxt = await c.call("desktop_monitors_list")
    border_red = st["overlay"]["state"] == "stopped" and st["overlay"]["visible"]
    r_ = await c.call("desktop_resume")
    record("T30 Esc (a user press, from another process) halts a running wait; border red; all calls refused until resume",
           err_code(res) == "KILL_SWITCH" and halt < 150 and st["kill_switch"]["source"] == "esc" and err_code(nxt) == "KILL_SWITCH" and border_red and r_.get("ok"),
           f"halted {halt:.0f} ms after the Esc (injected by an elevated helper process, not by the bridge); source={st['kill_switch']['source']!r}; border state={st['overlay']['state']} visible={st['overlay']['visible']}; next call -> {err_code(nxt)}")
    f = await c.call("desktop_find", query="TargetButton", window=w["hwnd"], role="Button")
    node = f["matches"][0]
    cx, cy = node["rect"][0] + node["rect"][2] // 2, node["rect"][1] + node["rect"][3] // 2
    ctypes.windll.user32.SetCursorPos(-3000, -1000)
    start = cursor()
    at = time.time() + 3.5
    run_helper("esc", at, out + "2.json")
    await sleep_until(at - 0.10)
    res = await c.call("desktop_click", ref=node["ref"])
    t_ret = time.time()
    info = await helper_result(out + "2.json")
    halt = (t_ret - info["t_down"][0]) * 1000
    cur = cursor()
    mid = abs(cur[0] - start[0]) + abs(cur[1] - start[1]) > 80 and abs(cur[0] - cx) + abs(cur[1] - cy) > 80
    await c.call("desktop_resume")
    hit = await wait_window(c, "clicks=1", 1.5)
    record("T31 Esc halts a mouse glide mid-way (no click happens)", err_code(res) == "KILL_SWITCH" and halt < 150 and mid and not hit,
           f"halted {halt:.0f} ms after the Esc; cursor stopped at {cur} (start {start}, target ({cx},{cy})) mid-way={mid}; click happened={bool(hit)}")
    await kill_pid(c, pid)
    pid, w = await launch_form(c, "password", 300, 450)
    f = await c.call("desktop_find", query="PlainBox", window=w["hwnd"], role="Edit")
    ref = f["matches"][0]["ref"]
    at = time.time() + 4.0
    run_helper("esc", at, out + "3.json")
    await sleep_until(at - 1.5)
    res = await c.call("desktop_type", text="0123456789" * 30, ref=ref)
    t_ret = time.time()
    info = await helper_result(out + "3.json")
    halt = (t_ret - info["t_down"][0]) * 1000
    await c.call("desktop_resume")
    v = (await c.call("desktop_find", query="PlainBox", window=w["hwnd"], role="Edit"))["matches"][0].get("value", "")
    record("T32 Esc halts typing mid-string (stop flag checked per character)", err_code(res) == "KILL_SWITCH" and halt < 150 and 20 < len(v) < 300, f"halted {halt:.0f} ms after the Esc; {len(v)} of 300 characters had been typed")
    await c.call("desktop_window_focus", hwnd=w["hwnd"])
    k = await c.call("desktop_key", combo="esc")
    await asyncio.sleep(0.3)
    st = await c.call("desktop_session_status")
    record("T33 the bridge's own injected Esc (desktop_key esc) does NOT trip the kill switch", k.get("ok") and not st["kill_switch"]["active"], f"key ok={k.get('ok')}; kill active={st['kill_switch']['active']}; esc armed={st['kill_switch']['esc']}")
    for _ in range(40):
        st = await c.call("desktop_session_status")
        if not st["in_control"]:
            break
        await asyncio.sleep(0.5)
    at = time.time() + 3.0
    run_helper("esc", at, out + "5.json")
    await helper_result(out + "5.json")
    await asyncio.sleep(0.6)
    st2 = await c.call("desktop_session_status")
    record("T34 Esc while Claude is idle (outside the control window) is ignored", not st["in_control"] and not st2["kill_switch"]["active"], f"in_control before={st['in_control']}; kill active after Esc={st2['kill_switch']['active']}")
    await kill_pid(c, pid)


async def t_self_protect(c: Client):
    out = os.path.join(TMP, f"helper-focus-{UID}.json")
    pid, w = await launch_form(c, "button", 1250, 500)
    try:
        run_helper("focus_feed", time.time() + 0.5, out)
        info = await helper_result(out)
        feed = await wait_window(c, FEED, 3)
        k = await c.call("desktop_key", combo="x")
        t = await c.call("desktop_type", text="x")
        fx, fy, fw, fh = feed["rect"]
        cl = await c.call("desktop_click", x=fx + fw // 2, y=fy + fh // 2, coords="screen")
        fo = await c.call("desktop_window_focus", hwnd=feed["hwnd"])
        ok = all(err_code(x) == "SELF_TARGET_REFUSED" for x in (k, t, cl, fo))
        record("T35 key/type/click/focus aimed at the bridge's own feed window are refused", ok, f"foreground was the feed={info.get('foreground_is_feed')}; key -> {err_code(k)}; type -> {err_code(t)}; click -> {err_code(cl)}; focus -> {err_code(fo)}")
        await c.call("desktop_window_focus", hwnd=w["hwnd"])
    finally:
        await kill_pid(c, pid)


async def t_yield(c: Client):
    """Shared environment: the bridge watches what the USER does and only stops when it actually affects the action."""
    out = os.path.join(TMP, f"helper-yield-{UID}")
    text = "0123456789" * 30

    async def typing_case(tag, action, **kw):
        pid, w = await launch_form(c, "password", 300, 450)
        f = await c.call("desktop_find", query="PlainBox", window=w["hwnd"], role="Edit")
        ref = f["matches"][0]["ref"]
        at = time.time() + 4.0
        run_helper(action, at, f"{out}-{tag}.json", **kw)
        await sleep_until(at - 1.5)
        res = await c.call("desktop_type", text=text, ref=ref)
        t_ret = time.time()
        info = await helper_result(f"{out}-{tag}.json")
        return pid, w, res, (t_ret - info["t_start"]) * 1000, info

    pid, w, res, ms, info = await typing_case("close", "close_window", title="BridgeTest password")
    st = await c.call("desktop_session_status")
    d = (res.get("error") or {}).get("data", {})
    record("T36 the user CLOSES the target window mid-typing: typing stops (TARGET_CLOSED), the error says what was typed, no kill lockout",
           err_code(res) == "TARGET_CLOSED" and ms < 200 and not st["kill_switch"]["active"] and d.get("target_alive") is False and 0 < d.get("typed", 0) < 300,
           f"-> {err_code(res)} {ms:.0f} ms after the close (WM_CLOSE posted: {bool(info.get('closed'))}); data: target_alive={d.get('target_alive')} typed={d.get('typed')} remaining={d.get('remaining')} foreground_now={d.get('foreground_now')}; kill active={st['kill_switch']['active']}")
    await kill_pid(c, pid)
    pid, w, res, ms, info = await typing_case("focus", "focus_feed")
    d = (res.get("error") or {}).get("data", {})
    record("T37 the user SWITCHES to another app mid-typing: typing stops (FOCUS_CHANGED), focus is NOT stolen back, the error names the app that has focus",
           err_code(res) == "FOCUS_CHANGED" and ms < 200 and d.get("target_alive") is True and "Desktop Bridge" in str(d.get("foreground_now")) and 0 < d.get("typed", 0) < 300,
           f"-> {err_code(res)} {ms:.0f} ms after the switch; data: foreground_now={d.get('foreground_now')} typed={d.get('typed')} remaining={d.get('remaining')}")
    await c.call("desktop_window_focus", hwnd=w["hwnd"])
    await kill_pid(c, pid)
    pid, w, res, ms, info = await typing_case("keys", "hold_key", vk=0x87, hold=700)
    d = (res.get("error") or {}).get("data", {})
    record("T39 the user presses keys in the same window while the bridge types: typing stops (USER_INTERVENTION), not a kill", err_code(res) == "USER_INTERVENTION" and 0x87 in d.get("user_keys_down", []) and 0 < d.get("typed", 0) < 300,
           f"-> {err_code(res)}; held keys seen={d.get('user_keys_down')}; typed={d.get('typed')} remaining={d.get('remaining')}")
    await kill_pid(c, pid)
    pid, w = await launch_form(c, "button", 1250, 500)
    f = await c.call("desktop_find", query="TargetButton", window=w["hwnd"], role="Button")
    node = f["matches"][0]
    ctypes.windll.user32.SetCursorPos(-3000, -1000)
    at = time.time() + 3.5
    run_helper("move_mouse", at, f"{out}-mouse.json", x=900, y=900)
    await sleep_until(at - 0.10)
    t0 = time.time()
    res = await c.call("desktop_click", ref=node["ref"])
    took = time.time() - t0
    await helper_result(f"{out}-mouse.json")
    await asyncio.sleep(0.4)
    hit = await wait_window(c, "clicks=1", 1.5)
    record("T38 the user BUMPS THE MOUSE during a glide: the bridge waits until the mouse is still, glides again and then clicks the right target", bool(res.get("ok") and hit and took > 0.5),
           f"click ok={res.get('ok')} after {took:.1f} s (includes waiting for the mouse to settle); target clicked={bool(hit)}")
    await kill_pid(c, pid)


# ============================================================================= M0.1: visibility policy

async def t_visibility(c: Client):
    nb = sum(1 for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "notepad.exe")
    h = await c.call("desktop_run", command="Start-Process notepad -WindowStyle Hidden")
    await asyncio.sleep(1.0)
    na = sum(1 for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "notepad.exe")
    h2 = await c.call("desktop_launch", app="powershell.exe", args=["-WindowStyle", "Hidden", "-Command", "Get-Date"])
    record("T40 hidden execution refused, nothing started", err_code(h) == "HIDDEN_EXECUTION_REFUSED" and err_code(h2) == "HIDDEN_EXECUTION_REFUSED" and na == nb,
           f"run -WindowStyle Hidden -> {err_code(h)}; launch with hidden args -> {err_code(h2)}; notepad processes {nb} -> {na}")
    d = await c.call("desktop_run", command="Invoke-WebRequest https://example.com -UseBasicParsing | Out-Null")
    d2 = await c.call("desktop_run", command="winget install --id Mozilla.Firefox --source winget")
    d3 = await c.call("desktop_launch", app="curl.exe", args=["https://example.com"])
    record("T41 downloads refused without user_requested_download (nothing fetched; the allow path is unit-tested only)", all(err_code(x) == "DOWNLOAD_NEEDS_USER_REQUEST" for x in (d, d2, d3)),
           f"Invoke-WebRequest -> {err_code(d)}; winget install -> {err_code(d2)}; launch curl -> {err_code(d3)}")
    sb = await c.call("desktop_run", command="[System.Windows.Forms.SendKeys]::SendWait('y')")
    record("T42 shell-based input synthesis refused (SendKeys etc.)", err_code(sb) == "INPUT_BYPASS_REFUSED", f"-> {err_code(sb)}")
    out = os.path.join(TMP, f"helper-min-{UID}.json")
    run_helper("minimize_feed", time.time() + 0.5, out)
    info = await helper_result(out)
    s_before = await c.call("desktop_session_status")
    await c.call("desktop_monitors_list")
    s_after = await c.call("desktop_session_status")
    record("T43 a minimized feed window is restored by the next call (no focus steal)", bool(info.get("minimized")) and s_before["feed_window"]["minimized"] and not s_after["feed_window"]["minimized"] and s_after["feed_window"]["visible"],
           f"minimized by helper={info.get('minimized')}; feed_window before={s_before['feed_window']}; after={s_after['feed_window']}")
    r = await c.call("desktop_launch", app="powershell.exe", wait_ms=9000,
                     args=["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(ROOT, "tests", "forms.ps1"), "-Mode", "minimized", "-AutoCloseSec", "25"])
    wins = [x for x in r.get("new_windows", []) if "BridgeTest minimized" in x["title"]]
    record("T44 launch: a window created minimized is restored and reported", bool(wins) and wins[0]["state"] == "normal" and wins[0]["restored_from_minimized"] and r.get("visible_window"),
           f"new windows={[(x['title'][:28], x['state'], x['restored_from_minimized']) for x in r.get('new_windows', [])]}")
    s = await c.call("desktop_session_status")
    ch = s["children"]
    bad = [x for x in ch if x.get("name", "").lower().startswith("pythonw") or x.get("hidden_windows")
           or (x.get("role") == "bridge-helper" and not x.get("on_feed_console")) or (x.get("role") == "launched-app" and not x.get("on_feed_console") and not x.get("visible_windows"))]
    record("T45 no hidden helper processes: the bridge's own helper shares the visible feed console; every app Claude launched shows a visible window; no pythonw, no hidden windows", not bad and "error" not in str(ch)[:80],
           f"children={[(x.get('name'), x.get('role'), 'console' if x.get('on_feed_console') else 'own-window' if x.get('visible_windows') else 'HIDDEN?') for x in ch]}; offenders={bad}")


# ============================================================================= separate sessions

async def t_launcher_kill(env):
    name = "T15 launcher killed mid-run: worker exits, pipe gone, no orphan"
    pipes_before = {p for p in os.listdir("\\\\.\\pipe\\") if p.startswith("desktop-bridge-")}
    py_before = sum(1 for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "python.exe")
    pr = subprocess.Popen([sys.executable, os.path.join(ROOT, "launcher.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env={**os.environ, **env})
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "1"}}})
    pr.stdin.write((init + "\n").encode())
    pr.stdin.flush()
    line = pr.stdout.readline()
    ok_init = b'"result"' in line
    during = {p for p in os.listdir("\\\\.\\pipe\\") if p.startswith("desktop-bridge-")} - pipes_before
    subprocess.run(["taskkill", "/PID", str(pr.pid), "/F", "/T"], capture_output=True)
    gone = False
    for _ in range(40):
        time.sleep(0.25)
        if not ({p for p in os.listdir("\\\\.\\pipe\\") if p.startswith("desktop-bridge-")} & during):
            gone = True
            break
    time.sleep(1.0)
    py_after = sum(1 for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "python.exe")
    record(name, bool(ok_init and during and gone and py_after <= py_before), f"initialize via second launcher ok={ok_init}; pipes appeared={len(during)} vanished={gone}; python.exe count {py_before} -> {py_after}")


async def s_overlay_fail(env):
    async with open_session({**env, "DESKTOP_BRIDGE_FORCE_OVERLAY_FAIL": "1"}) as c:
        s = await c.call("desktop_session_status")
        refused = {}
        for tool, args in [("desktop_click", {"x": 10, "y": 10, "coords": "screen"}), ("desktop_type", {"text": "x"}), ("desktop_key", {"combo": "f13"}),
                           ("desktop_launch", {"app": "notepad.exe"}), ("desktop_run", {"command": "echo hi"}), ("desktop_fs", {"op": "stat", "path": "C:\\Windows"}),
                           ("desktop_clipboard_set", {"text": "x"}), ("desktop_window_focus", {"title": "zzzz"})]:
            refused[tool] = err_code(await c.call(tool, **args))
        reads = {}
        for t, a in [("desktop_screenshot", {}), ("desktop_windows_list", {}), ("desktop_clipboard_get", {})]:
            reads[t] = (await c.call(t, **a)).get("ok")
        record("T50 overlay cannot be created -> input tools refuse OVERLAY_UNAVAILABLE (fail closed), read-only tools still work",
               not s["overlay"]["ok"] and all(v == "OVERLAY_UNAVAILABLE" for v in refused.values()) and all(reads.values()),
               f"overlay.ok={s['overlay']['ok']} error={s['overlay']['error'][:50]!r}; refused={refused}; read-only ok={reads}")


def _recycle_dir(path: str) -> None:
    try:
        import win32com.shell.shell as sh
        from win32com.shell import shellcon
        sh.SHFileOperation((0, shellcon.FO_DELETE, path + "\0", None, shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI, None, None))
    except Exception:
        pass


async def s_approval_pending():
    empty = tempfile.mkdtemp(prefix="bridge-approval-empty-")
    async with open_session({"DESKTOP_BRIDGE_APPROVAL_DIR": empty}) as c:
        s = await c.call("desktop_session_status")
        r = await c.call("desktop_monitors_list")
        f = await c.call("desktop_fs", op="stat", path="C:\\Windows")
        await asyncio.sleep(0.5)
        feed = feed_log_text()
        record("T55 unapproved/changed config.json: the worker asks in the feed window and serves NO tool until the user answers",
               err_code(r) == "CONFIG_APPROVAL_PENDING" and err_code(f) == "CONFIG_APPROVAL_PENDING" and not s["config_approved"] and "Approve changed config? (y/N)" in feed and "first start" in feed,
               f"config_approved={s['config_approved']}; monitors_list -> {err_code(r)}; fs -> {err_code(f)}; prompt + diff printed in the feed={('Approve changed config?' in feed)}")
    _recycle_dir(empty)


async def s_injection(env):
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    async with open_session(env) as c:
        pw, ww = await launch_form(c, "inject-weak", 700, 300, auto_close=40)
        weak = await c.call("desktop_ui_tree", window=ww["hwnd"] if ww else None, depth=3)
        s1 = await c.call("desktop_session_status")
        record("T60 weak injection-like wording: result labelled, logged, NOT locked", weak.get("ok") and weak.get("untrusted") and weak.get("injection_signals") and not s1["kill_switch"]["active"],
               f"ok={weak.get('ok')} untrusted={weak.get('untrusted')} signals={weak.get('injection_signals')} locked={s1['kill_switch']['active']}")
        ps, ws = await launch_form(c, "inject", 700, 600, auto_close=40)
        strong = await c.call("desktop_ui_tree", window=ws["hwnd"] if ws else None, depth=3)
        s2 = await c.call("desktop_session_status")
        nxt = await c.call("desktop_monitors_list")
        rs = await c.call("desktop_resume")
        await asyncio.sleep(3.0)
        feed = feed_log_text()
        lines = [ln for ln in tail_log(since) if "INJECTION lockout" in ln]
        record("T61 strong injection text read via ui_tree: content withheld, lockout engaged, red alert in the feed, logged, desktop_resume REFUSED",
               err_code(strong) == "INJECTION_LOCKOUT" and "Ignore all previous" not in json.dumps(strong) and s2["kill_switch"]["lockout"] == "injection" and err_code(nxt) == "KILL_SWITCH"
               and err_code(rs) == "INJECTION_LOCKOUT" and "INJECTION SUSPECTED" in feed and "Resume? (y/N)" in feed and bool(lines) and s2["overlay"]["state"] == "safety",
               f"ui_tree -> {err_code(strong)}; lockout={s2['kill_switch']['lockout']}; next call -> {err_code(nxt)}; resume -> {err_code(rs)}; feed alert={'INJECTION SUSPECTED' in feed}; prompt shown={'Resume? (y/N)' in feed}; log lines={len(lines)}; border state={s2['overlay']['state']}")


async def t_log(c: Client, since: str):
    await asyncio.sleep(3.0)
    lines = [ln for ln in tail_log(since) if "desktop_" in ln]
    missing = []
    for tool, n in CALLS.items():
        have = sum(1 for ln in lines if f"{tool} target=" in ln)
        if have < n:
            missing.append(f"{tool}:{have}/{n}")
    v = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(LOGDIR, "Verify-ClaudeLog.ps1")], capture_output=True, text=True)
    res = [ln for ln in v.stdout.splitlines() if ln.startswith("RESULT")]
    st = await c.call("desktop_session_status")
    acl = subprocess.run(["icacls", LOG], capture_output=True, text=True).stdout
    protected = "(DENY)" in acl or "(AD)" in acl
    record("T16 every tool call of the main session is in the actions log", not missing, f"{sum(CALLS.values())} calls made; missing={missing or 'none'}; writer stats={st.get('log')}")
    record("T17 Verify-ClaudeLog.ps1 still passes", bool(res and "PASS" in res[0]), f"{res[0] if res else v.stdout[-200:]}; log protection by the user: {'looks protected' if protected else 'NOT protected (FullControl); AppendData-only path untested'}")


async def main() -> int:
    part = sys.argv[1] if len(sys.argv) > 1 else "all"
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    store = make_store()
    env = {"DESKTOP_BRIDGE_APPROVAL_DIR": store}
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Desktop Bridge selftest ({part}), {since}. Windows open, the mouse moves, keys are typed. No input needed from you; hands off until it finishes.\n", flush=True)
    t_unit()
    steps_core = [lambda c, tools: t_startup(c, tools), lambda c, tools: t_coords(c), lambda c, tools: t_notepad(c), lambda c, tools: t_elevated_input(c),
                  lambda c, tools: t_clipboard(c), lambda c, tools: t_password(c), lambda c, tools: t_kill_combos(c), lambda c, tools: t_fs(c),
                  lambda c, tools: t_run_ui(c), lambda c, tools: t_audio(c), lambda c, tools: t_perf(c)]
    steps_control = [lambda c, tools: t_border(c), lambda c, tools: t_clickthrough(c), lambda c, tools: t_mouse(c), lambda c, tools: t_typing(c),
                     lambda c, tools: t_esc(c), lambda c, tools: t_self_protect(c), lambda c, tools: t_yield(c), lambda c, tools: t_visibility(c)]
    steps = (steps_core if part in ("all", "core") else []) + (steps_control if part in ("all", "control") else [])
    if steps:
        async with open_session(env) as c:
            tools = [t.name for t in (await c.s.list_tools()).tools]
            for step in steps:
                try:
                    await step(c, tools)
                except Exception as e:
                    record("EXCEPTION in test step", False, f"{type(e).__name__}: {e}")
                    try:
                        await c.call("desktop_resume")
                    except Exception:
                        pass
            await t_log(c, since)
    if part in ("all", "sessions"):
        await t_launcher_kill(env)
        for fn in (lambda: s_overlay_fail(env), lambda: s_approval_pending(), lambda: s_injection(env)):
            try:
                await fn()
            except Exception as e:
                record("EXCEPTION in session test", False, f"{type(e).__name__}: {e}")
    _recycle_dir(store)
    passed = sum(1 for _, ok, _ in RESULTS if ok is True)
    failed = [r for r in RESULTS if r[1] is False]
    skipped = [r for r in RESULTS if r[1] is None]
    print(f"\n{passed}/{passed + len(failed)} PASS ({len(skipped)} skipped on purpose)")
    for n, ok, d in failed:
        print("  FAILED:", n, "-", d)
    for n, ok, d in skipped:
        print("  SKIPPED:", n, "-", d)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))
