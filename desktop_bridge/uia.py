"""UI Automation tree, element refs, find. All calls must run on one COM-initialised thread (see tools.ui_executor)."""
from __future__ import annotations

import collections
import time

import uiautomation as auto

from .safety import ToolRefused

ROLE_STRIP = "Control"
MAX_REFS = 6000


def role_of(ctrl) -> str:
    n = ctrl.ControlTypeName or "Unknown"
    return n[:-len(ROLE_STRIP)] if n.endswith(ROLE_STRIP) else n


def rect_of(ctrl) -> tuple[int, int, int, int] | None:
    try:
        r = ctrl.BoundingRectangle
        if r is None:
            return None
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 0 or h <= 0:
            return None
        return (r.left, r.top, w, h)
    except Exception:
        return None


def is_password(ctrl) -> bool:
    try:
        return bool(ctrl.IsPassword)
    except Exception:
        return False


class RefTable:
    """Maps short element refs (e1, e2, ...) to live UIA controls. A control seen again (same runtime id) keeps its ref."""

    def __init__(self) -> None:
        self._by_ref: "collections.OrderedDict[str, object]" = collections.OrderedDict()
        self._by_key: dict[tuple, str] = {}
        self._n = 0

    def ref_for(self, ctrl) -> str:
        try:
            key = (ctrl.ProcessId, tuple(ctrl.GetRuntimeId() or ()))
        except Exception:
            key = None
        if key and key[1] and key in self._by_key and self._by_key[key] in self._by_ref:
            ref = self._by_key[key]
            self._by_ref[ref] = ctrl
            return ref
        self._n += 1
        ref = f"e{self._n}"
        self._by_ref[ref] = ctrl
        if key and key[1]:
            self._by_key[key] = ref
        while len(self._by_ref) > MAX_REFS:
            self._by_ref.popitem(last=False)
        return ref

    def get(self, ref: str):
        c = self._by_ref.get(ref)
        if c is None:
            raise ToolRefused("UNKNOWN_REF", f"Unknown or expired element ref '{ref}'. Call desktop_ui_tree or desktop_find again.")
        try:
            c.BoundingRectangle  # touches the COM element; raises if it no longer exists
        except Exception:
            raise ToolRefused("STALE_REF", f"Element '{ref}' no longer exists. Call desktop_ui_tree or desktop_find again.")
        return c


def value_of(ctrl, role: str) -> str | None:
    if is_password(ctrl):
        return "[redacted]"
    try:
        vp = ctrl.GetValuePattern() if hasattr(ctrl, "GetValuePattern") else None
        if vp is not None:
            v = vp.Value
            if v:
                return v[:300]
    except Exception:
        pass
    if role in ("Edit", "Document"):
        try:
            tp = ctrl.GetTextPattern()
            if tp is not None:
                t = tp.DocumentRange.GetText(400)
                if t:
                    return t
        except Exception:
            pass
    return None


def node_dict(ctrl, refs: RefTable, with_value: bool = True) -> dict:
    role = role_of(ctrl)
    d: dict = {"ref": refs.ref_for(ctrl), "role": role}
    name = ctrl.Name
    if name:
        d["name"] = name[:200]
    r = rect_of(ctrl)
    if r:
        d["rect"] = list(r)
    if with_value:
        v = value_of(ctrl, role)
        if v:
            d["value"] = v
    try:
        if not ctrl.IsEnabled:
            d["enabled"] = False
    except Exception:
        pass
    if is_password(ctrl):
        d["password"] = True
    aid = getattr(ctrl, "AutomationId", "")
    if aid:
        d["id"] = aid[:80]
    return d


def walk(root, refs: RefTable, depth: int, max_nodes: int, flt: str | None = None, include_offscreen: bool = False,
         deadline: float | None = None) -> tuple[dict, int, bool]:
    """Depth-limited walk. Returns (tree, node_count, truncated). Offscreen nodes are skipped unless requested."""
    count = 0
    truncated = False
    needle = flt.lower() if flt else None

    def matches(d: dict) -> bool:
        return needle in (d.get("name", "") + " " + d.get("role", "") + " " + d.get("value", "") + " " + d.get("id", "")).lower()

    def visit(c, level: int) -> dict | None:
        nonlocal count, truncated
        if count >= max_nodes or (deadline and time.monotonic() > deadline):
            truncated = True
            return None
        try:
            if not include_offscreen and level > 0 and c.IsOffscreen:
                return None
        except Exception:
            pass
        d = node_dict(c, refs)
        count += 1
        kids = []
        if level < depth:
            try:
                children = c.GetChildren()
            except Exception:
                children = []
            for ch in children:
                k = visit(ch, level + 1)
                if k is not None:
                    kids.append(k)
                if truncated and count >= max_nodes:
                    break
        elif level >= depth:
            try:
                if c.GetFirstChildControl() is not None:
                    d["more"] = True  # has children below the depth limit
            except Exception:
                pass
        if needle and not matches(d) and not kids:
            return None
        if kids:
            d["children"] = kids
        return d

    tree = visit(root, 0)
    return (tree or {}), count, truncated


def find(root, refs: RefTable, query: str, role: str | None = None, limit: int = 25, max_nodes: int = 4000,
         depth: int = 14, deadline: float | None = None) -> tuple[list[dict], bool]:
    q = query.lower()
    out: list[dict] = []
    seen = 0
    truncated = False
    stack = [(root, 0)]
    while stack:
        c, level = stack.pop(0)  # breadth-first: shallow matches first
        if seen >= max_nodes or (deadline and time.monotonic() > deadline):
            truncated = True
            break
        seen += 1
        try:
            name = c.Name or ""
            r = role_of(c)
            hay = (name + " " + (getattr(c, "AutomationId", "") or "")).lower()
            if q in hay and (not role or r.lower() == role.lower()) and rect_of(c):
                out.append(node_dict(c, refs))
                if len(out) >= limit:
                    break
        except Exception:
            pass
        if level < depth:
            try:
                for ch in c.GetChildren():
                    stack.append((ch, level + 1))
            except Exception:
                pass
    return out, truncated


def control_for_window(hwnd: int | None):
    if hwnd:
        return auto.ControlFromHandle(hwnd)
    c = auto.GetForegroundControl()
    if c is None:
        raise ToolRefused("NO_FOREGROUND", "No foreground window to read.")
    return c
