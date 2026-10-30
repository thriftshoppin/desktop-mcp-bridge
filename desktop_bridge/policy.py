"""Policy gates. ALL pattern checks here are best-effort tripwires, NOT security boundaries: a determined elevated process under
the same account can get around any software check. They exist to stop honest mistakes and cheap tricks, and to make the
user's rules (nothing hidden, no silent downloads, screen text is data) hard to break by accident.

* command_gate()      hidden execution / downloads / input-bypass / protected paths for `run` and `launch`
* scan_text()         instruction-injection signatures in text that came from the screen, files, clipboard or commands
* config approval     SHA-256 of config.json at the last user-approved start (the user answers in the feed window)
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .safety import ToolRefused

I = re.IGNORECASE
M = re.MULTILINE

# ----------------------------------------------------------------------------- hidden execution

HIDDEN = [
    (re.compile(r"-w\w*[\s:=]+['\"]?(?:hidden|minimi[sz]ed|h|mi|min)\b", I), "PowerShell/Start-Process -WindowStyle Hidden|Minimized"),
    (re.compile(r"windowstyle\s*[:=]\s*['\"]?(?:hidden|minimi[sz]ed)", I), "WindowStyle = Hidden|Minimized"),
    (re.compile(r"ProcessWindowStyle\]?\s*::?\s*(?:hidden|minimi[sz]ed)", I), "ProcessWindowStyle Hidden|Minimized"),
    (re.compile(r"CreateNoWindow", I), "CreateNoWindow"),
    (re.compile(r"\bstart\s+(?:\"[^\"]*\"\s*)?(?:/\w+\s+)*/(?:min|b)\b", I), "cmd start /min or /b"),
    (re.compile(r"\b(?:wscript|cscript)(?:\.exe)?\b[^\n]*//b\b", I), "wscript/cscript //B"),
    (re.compile(r"\bpythonw(?:\.exe)?\b", I), "pythonw"),
    (re.compile(r"\bmshta(?:\.exe)?\b", I), "mshta"),
    (re.compile(r"\b(?:Start-Job|Start-ThreadJob)\b|\s-AsJob\b", I), "hidden background job (Start-Job/-AsJob)"),
]
SCHEDULED = re.compile(r"\b(?:schtasks(?:\.exe)?\s+/create|Register-ScheduledTask|New-ScheduledTask(?:Action|Trigger)?|Set-ScheduledTask|"
                       r"sc(?:\.exe)?\s+(?:\\\\\S+\s+)?create|New-Service|Register-(?:Wmi|CimIndication)Event|Set-WmiInstance)\b", I)

# ----------------------------------------------------------------------------- downloads

DOWNLOAD = [
    (re.compile(r"\b(?:invoke-webrequest|iwr|invoke-restmethod|irm)\b", I), "Invoke-WebRequest/Invoke-RestMethod"),
    (re.compile(r"(?<![\w.\-])(?:curl|wget)(?:\.exe)?(?![\w\-.])", I), "curl/wget"),
    (re.compile(r"\bbitsadmin(?:\.exe)?\b", I), "bitsadmin"),
    (re.compile(r"\bcertutil(?:\.exe)?\b[^\n]*-urlcache", I), "certutil -urlcache"),
    (re.compile(r"\bStart-BitsTransfer\b", I), "Start-BitsTransfer"),
    (re.compile(r"\bWebClient\b|\bDownloadFile\b|\bDownloadString\b|\bDownloadData\b|\bHttpClient\b", I), "WebClient/DownloadFile"),
    (re.compile(r"\bpip3?(?:\.exe)?\s+install\b|\bpython\S*\s+-m\s+pip\s+install\b", I), "pip install"),
    (re.compile(r"\bnpm(?:\.cmd)?\s+(?:i|install|ci)\b", I), "npm install"),
    (re.compile(r"\bwinget(?:\.exe)?\s+install\b", I), "winget install"),
    (re.compile(r"\bchoco(?:latey)?(?:\.exe)?\s+install\b", I), "choco install"),
    (re.compile(r"\bgit(?:\.exe)?\s+clone\b", I), "git clone"),
]
URL = re.compile(r"\b(?:https?|ftp)://", I)

# ----------------------------------------------------------------------------- input bypass / self-target

INPUT_BYPASS = re.compile(r"\b(?:SendKeys|SendInput|keybd_event|mouse_event|AppActivate|SetForegroundWindow|PostMessage|SendMessage)\b", I)

READ_ONLY_START = re.compile(r"^\s*(?:Get-Content|gc|cat|type|more|Select-String|sls|Get-FileHash|Test-Path|Get-Item|Get-ItemProperty|dir|ls|Get-ChildItem|"
                             r"findstr|certutil\s+-hashfile)\b", I)


def _norm(s: str) -> str:
    return s.replace("/", "\\").lower()


def command_gate(text: str, protected_paths: list[str], user_requested_download: bool, downloads_mode: str = "ask",
                 where: str = "command") -> list[str]:
    """Raises ToolRefused or returns notes (e.g. ['DOWNLOAD user_requested']) for the feed/log."""
    notes: list[str] = []
    for rx, name in HIDDEN:
        if rx.search(text):
            raise ToolRefused("HIDDEN_EXECUTION_REFUSED", f"Refused: this {where} tries to hide or detach a process or window ({name}). "
                              "Everything the bridge does must be visible to the user. (Best-effort check, not a security boundary.)")
    if SCHEDULED.search(text):
        if not user_requested_download:
            raise ToolRefused("HIDDEN_EXECUTION_REFUSED", f"Refused: this {where} creates a scheduled task/service (a hidden background job). "
                              "Only allowed when the user asked for it in chat and the call passes user_requested_download=true.")
        notes.append("SCHEDULED_TASK_OR_SERVICE user_requested")
    if INPUT_BYPASS.search(text):
        raise ToolRefused("INPUT_BYPASS_REFUSED", f"Refused: this {where} synthesises keyboard/mouse input or activates windows by itself "
                          "(SendKeys/SendInput/keybd_event/AppActivate...). Use desktop_click / desktop_type / desktop_key, which are visible, "
                          "logged and kill-switch aware.")
    hits = [name for rx, name in DOWNLOAD if rx.search(text)]
    if hits:
        if downloads_mode == "never" or not user_requested_download:
            raise ToolRefused("DOWNLOAD_NEEDS_USER_REQUEST", f"Refused: this {where} fetches from the network ({', '.join(hits)}). "
                              "Downloads/installs happen only when the user's own chat message asks for exactly that; then pass "
                              "user_requested_download=true. Installs go through winget (exact package id, --source winget); anything else: "
                              "open the official page and let the user click Download.")
        notes.append("DOWNLOAD user_requested: " + ", ".join(hits))
    low = _norm(text)
    for p in protected_paths:
        pn = _norm(os.path.expandvars(p)).rstrip("\\")
        if pn and pn in low and not READ_ONLY_START.match(text):
            raise ToolRefused("PROTECTED_PATH", f"Refused: this {where} touches a protected policy/log path ({os.path.basename(pn) or pn}) "
                              "and does not look read-only. (Best-effort check, not a security boundary.)")
    return notes


def fs_gate(paths: list[str], user_requested_download: bool, downloads_mode: str = "ask") -> list[str]:
    notes = []
    for p in paths:
        if p and URL.search(p):
            if downloads_mode == "never" or not user_requested_download:
                raise ToolRefused("DOWNLOAD_NEEDS_USER_REQUEST", "Refused: fs path is a network URL.")
            notes.append("DOWNLOAD user_requested: fs " + p[:80])
    return notes


LAUNCH_SHELLS = {"powershell", "pwsh", "cmd", "python", "python3", "py", "pip", "pip3", "npm", "winget", "choco", "git", "curl", "wget",
                 "bitsadmin", "certutil", "wscript", "cscript", "mshta", "pythonw", "node", "bash", "wsl", "sh"}


def launch_gate(app: str, args: list[str] | None, protected_paths: list[str], flag: bool, mode: str) -> list[str]:
    base = os.path.splitext(os.path.basename(app.strip('"')))[0].lower()
    text = " ".join([app] + [a for a in (args or [])])
    if base in LAUNCH_SHELLS or any(HIDDEN_RX.search(text) for HIDDEN_RX in (h[0] for h in HIDDEN)):
        return command_gate(text, protected_paths, flag, mode, where="launch")
    return []


# ----------------------------------------------------------------------------- injection scanner

S = [
    ("override-instructions", re.compile(r"\b(?:ignore|disregard|forget|override|bypass)\b[^\n.]{0,50}?\b(?:all|any|every|previous|prior|above|earlier|preceding|your|the|these|those|my|safety)\b"
                                         r"[^\n.]{0,50}?\b(?:instructions?|prompts?|rules|guidelines|directives|restrictions|polic(?:y|ies)|system)\b", I)),
    ("role-reset", re.compile(r"\byou are now (?:an? |the |in )?(?:ai\b|assistant|developer mode|dan\b|unrestricted|jailbroken|root\b|admin|evil|free\b|a different|the new)", I)),
    ("new-system-prompt", re.compile(r"\bnew system prompt\b|\bsystem prompt\s*[:=]", I)),
    ("fake-turn", re.compile(r"^\s*[\[<]?\s*(?:system|assistant|developer|human|user)\s*[\]>]?\s*:\s*(?:you\b|ignore|from now|new instructions|please|run\b|execute|disregard|do not|don't|forget)", I | M)),
    ("chat-template-markers", re.compile(r"<\|?(?:im_start|im_end|system|assistant|endoftext)\|?>|\[/?INST\]|<<SYS>>|###\s*(?:system|instruction)s?\b", I)),
    ("addresses-the-ai", re.compile(r"\bas (?:an? )?(?:ai|assistant|language model)[, ]+you (?:must|should|will|need)|"
                                    r"\b(?:dear|hey|hello|attention|note to|message for|instructions? for)\s+(?:claude|ai|assistant|chatgpt|llm|agent|model)\b|"
                                    r"\b(?:claude|assistant|ai agent)\b[,:]?\s+(?:you must|you should|you need to|you will|please (?:run|execute|type|click|open|download|send|delete|ignore)|now (?:run|execute|type|click|open|download|send|delete|ignore))", I)),
    ("run-this-command-at-you", re.compile(r"\byou\b[^.\n]{0,60}\b(?:run|execute|paste|type|enter)\b[^.\n]{0,40}\bfollowing\b[^.\n]{0,20}\b(?:command|script|code|text)\b|"
                                           r"\b(?:run|execute)\s+the\s+following\s+(?:command|script|code)\b[^.\n]{0,100}\b(?:silently|without (?:telling|asking|the user)|don't (?:tell|mention)|secretly)\b", I)),
    ("secrecy", re.compile(r"\b(?:do not|don't|never)\s+(?:tell|inform|mention|show|reveal)\s+(?:this to\s+)?the user\b|without (?:the user|them) (?:knowing|noticing)", I)),
]
BLOB = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
EXEC_HINT = re.compile(r"\b(?:powershell|iex|invoke-expression|frombase64string|-enc(?:odedcommand)?|eval|exec)\b", I)
WEAK = [
    ("mentions-ai-or-prompts", re.compile(r"\b(?:ai assistant|language model|prompt injection|system prompt|jailbreak)\b", I)),
    ("you-must-run", re.compile(r"\byou must\b.{0,30}\b(?:run|execute|type|click)\b", I)),
]
SKIP_KEYS = {"untrusted_note", "note", "coords", "ok", "error", "ref", "rect", "hwnd", "pid", "state", "role", "id"}


def _strings(obj, path="", depth=0):
    if depth > 40:
        return
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in SKIP_KEYS:
                continue
            yield from _strings(v, f"{path}.{k}" if path else k, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]", depth + 1)


def _excerpt(text: str, m: re.Match, n: int = 120) -> str:
    a = max(0, m.start() - 20)
    return text[a:a + n].encode("unicode_escape").decode("ascii")[:n]


def scan_text(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """-> (strong[(signature, excerpt)], weak[signature])"""
    strong: list[tuple[str, str]] = []
    for name, rx in S:
        m = rx.search(text)
        if m:
            strong.append((name, _excerpt(text, m)))
    b = BLOB.search(text)
    if b and EXEC_HINT.search(text):
        strong.append(("encoded-blob-with-exec-hint", _excerpt(text, b)))
    weak = [name for name, rx in WEAK if rx.search(text)]
    if b and not any(n == "encoded-blob-with-exec-hint" for n, _ in strong):
        weak.append("long-encoded-blob")
    return strong, weak


def scan_result(result: dict) -> tuple[list[dict], list[str]]:
    """Scan every string value of a tool result. -> (strong hits[{where,signature,excerpt}], weak names)"""
    hits: list[dict] = []
    weak_all: list[str] = []
    for path, s in _strings(result):
        if len(s) < 8:
            continue
        strong, weak = scan_text(s)
        for name, ex in strong:
            hits.append({"where": path, "signature": name, "excerpt": ex})
        weak_all.extend(w for w in weak if w not in weak_all)
    return hits, weak_all


# ----------------------------------------------------------------------------- config approval

def config_digest(root: str) -> tuple[str, dict]:
    h = hashlib.sha256()
    merged: dict = {}
    for name in ("config.json", "config.local.json"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            raw = open(p, "rb").read()
            h.update(name.encode() + b"\0" + raw + b"\0")
            try:
                merged[name] = json.loads(raw.decode("utf-8-sig"))
            except Exception:
                merged[name] = {"_unparseable": True}
    return h.hexdigest(), merged


def _flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = d
    return out


def diff_summary(old: dict | None, new: dict) -> list[str]:
    if old is None:
        return ["no previously approved config on record (first start)"]
    a, b = _flatten(old), _flatten(new)
    lines = []
    for k in sorted(set(a) | set(b)):
        if a.get(k, "<missing>") != b.get(k, "<missing>"):
            lines.append(f"{k}: {json.dumps(a.get(k, '<missing>'))[:60]} -> {json.dumps(b.get(k, '<missing>'))[:60]}")
    return lines[:30] or ["content changed but no key differences (formatting only)"]


class Approval:
    """Remembers the config hash the user last approved. Store lives outside the repo and is on the protected-paths list."""

    def __init__(self, root: str, store_dir: str | None = None):
        self.root = root
        # %ProgramData% is not redirected for packaged (MSIX) parents, unlike %LOCALAPPDATA%: the store stays in one place for every client
        self.dir = store_dir or os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "DesktopBridge", "state")
        self.path = os.path.join(self.dir, "config.approved.json")

    def state(self) -> tuple[bool, list[str], str]:
        """-> (approved, diff lines, digest)"""
        digest, merged = config_digest(self.root)
        old = None
        if os.path.exists(self.path):
            try:
                rec = json.load(open(self.path, encoding="utf-8"))
                if rec.get("sha256") == digest:
                    return True, [], digest
                old = rec.get("config")
            except Exception:
                old = None
        return False, diff_summary(old, merged), digest

    @staticmethod
    def legacy_dirs() -> list[str]:
        import glob
        la = os.environ.get("LOCALAPPDATA", "")
        return [os.path.join(la, "desktop-bridge")] + glob.glob(os.path.join(la, "Packages", "Claude_*", "LocalCache", "Local", "desktop-bridge"))

    def import_legacy(self) -> str | None:
        """One-time migration: if an earlier version recorded the user's approval of THIS exact config (same SHA-256) in the old location, adopt it.
        Never imports an approval for a different config. Returns the source folder or None."""
        digest, _ = config_digest(self.root)
        for d in self.legacy_dirs():
            p = os.path.join(d, "config.approved.json")
            try:
                with open(p, encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                continue
            if rec.get("sha256") == digest:
                os.makedirs(self.dir, exist_ok=True)
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(rec, f, indent=1)
                return d
        return None

    def approve(self) -> None:
        digest, merged = config_digest(self.root)
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"sha256": digest, "config": merged}, f, indent=1)
