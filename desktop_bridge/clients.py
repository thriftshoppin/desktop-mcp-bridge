"""Client identity and the one-time 'Allow this client?' allow-list for the shared guardian.

A client is the program that started a launcher (Claude desktop, Claude Code in VS Code, a test script...). The guardian derives its
identity from the kernel-reported launcher PID: the first ancestor that is not a launcher/venv shim. The allow-list lives in the
guardian's protected state folder; an unknown identity makes the guardian ask the user in the feed window (typed y/N).
Honest limit: a same-user process can spoof its parent; with silent UAC elevation such a process could elevate anyway, so this is a
consent/accounting layer (and per-client audit labels), not a security boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading

SCRIPT_HOSTS = {"python.exe", "pythonw.exe", "node.exe", "powershell.exe", "pwsh.exe", "cmd.exe"}


# Claude desktop (Cowork) is the PRIMARY client: what most people use this bridge with. When two clients want the pointer or keyboard at the same
# moment, the primary goes first and the others wait briefly (they are never refused for that).
PRIMARY_LABELS = {"Claude desktop"}


def is_primary(label: str) -> bool:
    return label in PRIMARY_LABELS


def norm_path(p: str) -> str:
    """Lower-case, forward slashes, version-like digit runs collapsed so updates (Claude_4.4212.0.0, claude-code-2.1.312) keep the identity."""
    p = (p or "").replace("\\", "/").lower()
    return re.sub(r"\d+(?:\.\d+)+", "#", p)


def identity_key(client_exe: str, script: str = "") -> str:
    exe = norm_path(client_exe)
    name = exe.rsplit("/", 1)[-1]
    sc = os.path.basename((script or "").replace("\\", "/")).lower() if name in SCRIPT_HOSTS else ""
    return hashlib.sha256(f"{exe}|{sc}".encode()).hexdigest()[:32]


def label_for(client_exe: str, script: str = "") -> str:
    p = (client_exe or "").replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    if "/windowsapps/claude_" in p and name == "claude.exe":
        return "Claude desktop"
    if "anthropic.claude-code" in p and name == "claude.exe":
        return "Claude Code (VS Code)"
    if name in SCRIPT_HOSTS and script:
        return f"{name} {os.path.basename(script)}"
    return os.path.basename(client_exe or "") or "unknown client"


def find_client(pid: int) -> dict:
    """Walk up from the launcher process to the program that started it. Needs psutil; works from the elevated guardian for any process."""
    import psutil
    try:
        p = psutil.Process(pid)
    except Exception:
        return {"exe": "", "script": "", "pid": 0, "label": "unknown client", "key": identity_key("")}
    hops = 0
    cur = p
    while hops < 8:
        try:
            cmd = [c.lower() for c in cur.cmdline()]
        except Exception:
            cmd = []
        if not any(c.endswith("launcher.py") for c in cmd):  # launcher and its venv shim both run launcher.py: keep walking up
            break
        cur = cur.parent()
        if cur is None:
            break
        hops += 1
    try:
        exe = cur.exe()
    except Exception:
        exe = cur.name() if cur is not None else ""
    script = ""
    try:
        for c in cur.cmdline()[1:]:
            if c.lower().endswith((".py", ".js", ".ps1")):
                script = c
                break
    except Exception:
        pass
    return {"exe": exe, "script": script, "pid": cur.pid if cur is not None else 0, "label": label_for(exe, script), "key": identity_key(exe, script)}


class AllowList:
    def __init__(self, store_dir: str):
        self.path = os.path.join(store_dir, "clients.json")
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def is_allowed(self, key: str) -> bool:
        return key in self._load()

    def add(self, key: str, label: str, exe: str) -> None:
        with self._lock:
            d = self._load()
            d[key] = {"label": label, "exe": exe}
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)

    def remove(self, key: str) -> bool:
        with self._lock:
            d = self._load()
            if key not in d:
                return False
            del d[key]
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            return True

    def entries(self) -> dict:
        return self._load()
