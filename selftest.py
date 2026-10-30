"""Desktop MCP Bridge M0 self-test. Drives the server through a real MCP stdio client, via the LAUNCHER (so the elevated path
is exercised). Everything happens in visible windows. Run: .venv\\Scripts\\python.exe selftest.py"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from desktop_bridge import winapi  # noqa: E402

LOG = r"C:\Users\SysAdmin\Claude\claude-actions-log.md"
LOGDIR = r"C:\Users\SysAdmin\Claude\log-tools"
TMP = os.path.expandvars(r"%TEMP%\desktop-bridge")
RESULTS: list[tuple[str, bool, str]] = []
CALLS: dict[str, int] = {}
UID = uuid.uuid4().hex[:8]


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


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
        data["_is_error"] = res.is_error
        return data


def err_code(d: dict) -> str:
    return (d.get("error") or {}).get("code", "")


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


async def launch_form(c: Client, mode: str, x: int, y: int):
    script = os.path.join(ROOT, "tests", "forms.ps1")
    d = await c.call("desktop_launch", app="powershell.exe", wait_ms=500,
                     args=["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Mode", mode, "-X", str(x), "-Y", str(y)])
    w = await wait_window(c, f"BridgeTest {mode}", 15)
    return d.get("pid"), w


def tail_log(since: str) -> list[str]:
    with open(LOG, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return [ln for ln in lines if ln.startswith("- ") and ln[2:21] >= since]


# ----------------------------------------------------------------------------- tests

async def t_startup(c: Client, tools: list[str]):
    expected = {"desktop_session_status", "desktop_monitors_list", "desktop_screenshot", "desktop_zoom", "desktop_windows_list",
                "desktop_window_focus", "desktop_ui_tree", "desktop_find", "desktop_click", "desktop_type", "desktop_key",
                "desktop_wait", "desktop_launch", "desktop_clipboard_get", "desktop_clipboard_set", "desktop_run", "desktop_fs",
                "desktop_audio_level", "desktop_resume"}
    record("T01 tool list", expected.issubset(set(tools)), f"{len(tools)} tools, missing={sorted(expected - set(tools))}")
    s = await c.call("desktop_session_status")
    w = s.get("worker", {})
    record("T02 worker is elevated (via launcher, silent runas)", bool(w.get("elevated")) and w.get("integrity") == "high",
           f"elevated={w.get('elevated')} integrity={w.get('integrity')} type={s.get('type')} locked={s.get('locked')} hotkeys={s.get('kill_switch', {}).get('hotkeys')}")
    win = await wait_window(c, "Desktop Bridge - live action feed", 5)
    record("T03 worker window is VISIBLE (live action feed)", win is not None and win["state"] != "minimized",
           f"{'found hwnd ' + str(win['hwnd']) + ' state=' + win['state'] if win else 'not found'}")


def t_unit():
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "tests"], cwd=ROOT, capture_output=True, text=True)
    last = (r.stderr.strip().splitlines() or [""])[-1]
    ran = [ln for ln in r.stderr.splitlines() if ln.startswith("Ran ")]
    record("T04 coordinate/denylist unit tests", r.returncode == 0, f"{ran[0] if ran else ''} {last}")


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
            d1 = await c.call("desktop_click", ref=node["ref"])
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
            record(name, ok, f"window on monitor={on_mon} ref-click={'ok' if w1 else 'MISS'} shot-click={'ok' if w2 else 'MISS'} "
                   f"shot=({sx:.1f},{sy:.1f}) scale={sc:.4f} -> real ({d2.get('clicked')}) vs target ({cx},{cy}) drift={drift}px engine={shot.get('engine')}")
        finally:
            await kill_pid(c, pid)
            await asyncio.sleep(0.5)


async def t_notepad(c: Client):
    name = "T06 Notepad round trip (launch, type, ui_tree shows text, ctrl+s, fs read matches, close tab)"
    os.makedirs(TMP, exist_ok=True)
    path = os.path.join(TMP, f"selftest-notepad-{UID}.txt")
    text = "bridge selftest \u00fcn\u00ef \u2713 123"
    await c.call("desktop_fs", op="write", path=path, content="")
    d = await c.call("desktop_launch", app="notepad.exe", args=[path], wait_ms=1500)
    w = await wait_window(c, f"selftest-notepad-{UID}", 12)
    if not w:
        record(name, False, "Notepad window with our file did not appear")
        return
    await c.call("desktop_window_focus", hwnd=w["hwnd"])
    f = await c.call("desktop_find", query="", window=w["hwnd"], role="Document", limit=3)
    if not f.get("matches"):
        f = await c.call("desktop_find", query="", window=w["hwnd"], role="Edit", limit=3)
    if not f.get("matches"):
        tree = await c.call("desktop_ui_tree", window=w["hwnd"], depth=5, max_nodes=120)
        record(name, False, "no Document/Edit element; tree nodes=" + str(tree.get("nodes")))
        return
    ref = f["matches"][0]["ref"]
    t = await c.call("desktop_type", text=text, ref=ref)
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
    record(name, bool(shows and got is not None), f"typed via {t.get('method')}; ui_tree shows text={shows}; file exact match={got is not None}; tab closed={gone is None}")
    await c.call("desktop_fs", op="delete", path=path)


async def t_elevated_input(c: Client):
    name = "T07 input into an ELEVATED window (Task Manager)"
    pre = await c.call("desktop_windows_list")
    if any(w["process"].lower() == "taskmgr.exe" for w in pre.get("windows", [])):
        record(name, False, "SKIPPED: Task Manager was already open (not touching the user's window)")
        return
    d = await c.call("desktop_launch", app="taskmgr.exe", wait_ms=4000)
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
        tree = await c.call("desktop_ui_tree", window=w["hwnd"], depth=6, max_nodes=200)
        await c.call("desktop_key", combo="alt+f4")
        record(name, False, f"search box not found (nodes={tree.get('nodes')})")
        return
    ref = f["matches"][0]["ref"]
    c1 = await c.call("desktop_click", ref=ref)
    t = await c.call("desktop_type", text="z", replace=False)
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
    name = "T08 clipboard round trip (unicode)"
    saved = await c.call("desktop_clipboard_get")
    sample = "h\u00e9llo \u2713 \u65e5\u672c\u8a9e \U0001F600 caf\u00e9"
    await c.call("desktop_clipboard_set", text=sample)
    got = await c.call("desktop_clipboard_get")
    await c.call("desktop_clipboard_set", text=saved.get("text", ""))
    record(name, got.get("text") == sample, f"round trip {'exact' if got.get('text') == sample else 'MISMATCH ' + repr(got.get('text'))}; user's clipboard restored")


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
        flagged = bool(pwf["matches"][0].get("password"))
        r1 = await c.call("desktop_type", text=secret, ref=pwref)
        await c.call("desktop_click", ref=pwref)
        r2 = await c.call("desktop_type", text=secret)  # no ref: the focused field is the password box
        r3 = await c.call("desktop_type", text="plain-ok", ref=plref)
        await asyncio.sleep(3.0)
        lines = tail_log(since)
        mine = [ln for ln in lines if "desktop_type" in ln]
        leaked = any(secret in ln for ln in lines)
        redacted = any("[redacted]" in ln and "PASSWORD_FIELD" in ln for ln in mine)
        ok = (err_code(r1) == "PASSWORD_FIELD" and err_code(r2) == "PASSWORD_FIELD" and r3.get("ok") and redacted and not leaked)
        record(name, bool(ok), f"UIA IsPassword flagged={flagged}; by ref -> {err_code(r1)}; by focus -> {err_code(r2)}; plain box ok={r3.get('ok')}; "
               f"log has [redacted]+PASSWORD_FIELD={redacted}; secret in log={leaked}")
    finally:
        await kill_pid(c, pid)


async def t_kill(c: Client):
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
        ok = (err_code(res) == "KILL_SWITCH" and err_code(nxt) == "KILL_SWITCH" and st["kill_switch"]["active"] and res_.get("was_active") and after.get("ok"))
        record(name, bool(ok), f"wait halted in {halt_ms:.0f} ms ({err_code(res)}); next call -> {err_code(nxt)}; status shows active={st['kill_switch']['active']}; after resume ok={after.get('ok')}")


async def t_fs(c: Client):
    name = "T11 fs: write/read/copy/move, delete -> Recycle Bin, protected paths refused"
    os.makedirs(TMP, exist_ok=True)
    base = os.path.join(TMP, f"bridge-selftest-{UID}")
    p1, p2, p3 = base + ".txt", base + "-copy.txt", base + "-moved.txt"
    w = await c.call("desktop_fs", op="write", path=p1, content="line one\n\u00fcn\u00ef\n")
    r = await c.call("desktop_fs", op="read", path=p1)
    cp = await c.call("desktop_fs", op="copy", path=p1, dest=p2)
    mv = await c.call("desktop_fs", op="move", path=p2, dest=p3)
    ls = await c.call("desktop_fs", op="list", path=TMP)
    d1 = await c.call("desktop_fs", op="delete", path=p1)
    d3 = await c.call("desktop_fs", op="delete", path=p3)
    gone = not os.path.exists(p1) and not os.path.exists(p3)
    rb = await c.call("desktop_run", command="(New-Object -ComObject Shell.Application).NameSpace(10).Items() | Where-Object { $_.Name -like 'bridge-selftest-" + UID + "*' } | ForEach-Object { $_.Name }")
    in_bin = len([ln for ln in rb.get("stdout", "").splitlines() if ln.strip()])
    prot = await c.call("desktop_fs", op="write", path=LOG, content="x", append=True)
    prot2 = await c.call("desktop_fs", op="delete", path=os.path.join(LOGDIR, "Add-ClaudeLog.ps1"))
    ok = (r.get("content", "").startswith("line one") and cp.get("ok") and mv.get("ok") and w.get("ok") and gone and in_bin >= 2
          and err_code(prot) == "PROTECTED_PATH" and err_code(prot2) == "PROTECTED_PATH" and any(e["name"].startswith(os.path.basename(base)) for e in ls.get("entries", [])))
    record(name, bool(ok), f"files gone from disk={gone}; found in Recycle Bin={in_bin}/2; log write -> {err_code(prot)}; log-tool delete -> {err_code(prot2)}")


async def t_run_ui(c: Client):
    r = await c.call("desktop_run", command="Write-Output 'h\u00e9llo \u2713'; $PSVersionTable.PSVersion.Major; exit 3")
    z = await c.call("desktop_zoom", region=[0, 0, 300, 200], coords="screen")
    wl = await c.call("desktop_windows_list")
    tree = await c.call("desktop_ui_tree", depth=4, max_nodes=300)
    timeout_r = await c.call("desktop_run", command="Start-Sleep -Seconds 20", timeout=2)
    record("T12 run/zoom/windows_list/ui_tree basics",
           r.get("exit_code") == 3 and "h\u00e9llo \u2713" in r.get("stdout", "") and z.get("_images") == 1 and wl.get("count", 0) > 0 and tree.get("nodes", 0) > 0 and timeout_r.get("timed_out"),
           f"run exit={r.get('exit_code')} out={r.get('stdout', '').strip()[:20]!r}; zoom image={z.get('_images')} engine={z.get('engine')}; windows={wl.get('count')} "
           f"(non_target={sum(1 for w in wl.get('windows', []) if w.get('non_target'))}); ui_tree nodes={tree.get('nodes')} in {tree.get('elapsed_ms')} ms; timeout kill={timeout_r.get('timed_out')}")


async def t_audio(c: Client):
    a = await c.call("desktop_audio_level")
    shot = await c.call("desktop_screenshot")  # dxcam + PyAudioWPatch both loaded in one worker: still capturing
    record("T13 audio_level picks the device with signal (+ capture still works with both libs loaded)", a.get("ok") and shot.get("ok") and shot.get("engine", "").startswith("dxcam"),
           f"device={a.get('device')!r} peak={a.get('peak')} playing={a.get('playing')} default={a.get('default_device')!r}; screenshot engine={shot.get('engine')}; "
           f"top devices={[(d['device'][:22], d['peak']) for d in a.get('devices', [])[:3]]}")


async def t_perf(c: Client):
    ts = []
    for _ in range(5):
        t0 = time.perf_counter()
        d = await c.call("desktop_screenshot")
        ts.append((time.perf_counter() - t0) * 1000)
    tv = await c.call("desktop_screenshot", monitor=1)
    record("T14 screenshot timing", True, f"1080p x5: median {sorted(ts)[2]:.0f} ms (engine {d.get('engine')}); 4K monitor: engine {tv.get('engine')} {tv.get('image_size')}")


async def t_launcher_kill():
    name = "T15 launcher killed mid-run: worker exits, pipe gone, no orphan"
    pipes_before = {p for p in os.listdir("\\\\.\\pipe\\") if p.startswith("desktop-bridge-")}
    pyw_before = sum(1 for p in __import__("psutil").process_iter(["name"]) if p.info["name"] and p.info["name"].lower() in ("python.exe",))
    pr = subprocess.Popen([sys.executable, os.path.join(ROOT, "launcher.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
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
    pyw_after = sum(1 for p in __import__("psutil").process_iter(["name"]) if p.info["name"] and p.info["name"].lower() in ("python.exe",))
    record(name, bool(ok_init and during and gone and pyw_after <= pyw_before), f"initialize via second launcher ok={ok_init}; its pipes appeared={len(during)} and vanished={gone}; python.exe count before={pyw_before} after={pyw_after}")


async def t_log(c: Client, since: str):
    await asyncio.sleep(3.0)
    lines = [ln for ln in tail_log(since) if "desktop_" in ln and "desktop-bridge" not in ln[:0]]
    missing = []
    for tool, n in CALLS.items():
        have = sum(1 for ln in lines if f"{tool} target=" in ln)
        if have < n:
            missing.append(f"{tool}:{have}/{n}")
    v = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(LOGDIR, "Verify-ClaudeLog.ps1")], capture_output=True, text=True)
    res = [ln for ln in v.stdout.splitlines() if ln.startswith("RESULT")]
    st = await c.call("desktop_session_status")
    acl = subprocess.run(["icacls", LOG], capture_output=True, text=True).stdout
    protected = "(DENY)" in acl or "(AD)" in acl or "(I)(AD)" in acl
    record("T16 every tool call is in the actions log", not missing, f"{sum(CALLS.values())} calls made; missing={missing or 'none'}; writer stats={st.get('log')}")
    record("T17 Verify-ClaudeLog.ps1 still passes", bool(res and "PASS" in res[0]), f"{res[0] if res else v.stdout[-200:]}; log protection applied by user: {'looks protected' if protected else 'NOT protected (FullControl); AppendData-only path untested'}")


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Desktop Bridge M0 selftest, {since}. Windows will open; hands off the mouse/keyboard until it finishes.\n", flush=True)
    t_unit()
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT, "launcher.py")], cwd=ROOT)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            c = Client(s)
            tools = [t.name for t in (await s.list_tools()).tools]
            for step in (lambda: t_startup(c, tools), lambda: t_coords(c), lambda: t_notepad(c), lambda: t_elevated_input(c), lambda: t_clipboard(c),
                         lambda: t_password(c), lambda: t_kill(c), lambda: t_fs(c), lambda: t_run_ui(c), lambda: t_audio(c), lambda: t_perf(c)):
                try:
                    await step()
                except Exception as e:  # keep going; report
                    record("EXCEPTION in test step", False, f"{type(e).__name__}: {e}")
                    try:
                        await c.call("desktop_resume")
                    except Exception:
                        pass
            await t_log(c, since)
    await t_launcher_kill()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} PASS")
    for n, ok, d in RESULTS:
        if not ok:
            print("  FAILED:", n, "-", d)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))
