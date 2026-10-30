"""Loads config.json (repo root) merged with an optional config.local.json (git-ignored overrides)."""
from __future__ import annotations

import copy
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(root: str = ROOT) -> dict:
    cfg: dict = {}
    for name in ("config.json", "config.local.json"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                cfg = _merge(cfg, json.load(f))
    cfg["temp_dir"] = os.path.expandvars(cfg.get("temp_dir", r"%TEMP%\desktop-bridge"))
    return cfg
