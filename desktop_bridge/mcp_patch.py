"""Work around an mcp 2.3.0 quirk: MCPServer pre-parses JSON-looking STRING arguments ('{}', '[1,2]', '{"a":1}', 'null') unless the parameter is
annotated exactly `str`. For our `str | None` parameters (fs content, clipboard/type text, run command...) that turns file content like {} into a dict and the
call fails validation, so writing JSON files through the bridge would break. Parameters whose annotation includes `str` are never pre-parsed.
Parameters that are lists/numbers (region, point, modifiers, args...) keep the SDK behaviour (Claude Desktop sometimes sends them as JSON strings)."""
from __future__ import annotations

import typing

from mcp.server.mcpserver.utilities import func_metadata as fm

_orig = fm.FuncMetadata.pre_parse_json
_applied = False


def _accepts_str(ann) -> bool:
    return ann is str or str in typing.get_args(ann)


def _pre_parse(self, data: dict) -> dict:
    keep = {}
    rest = {}
    for k, v in data.items():
        f = self.arg_model.model_fields.get(k)
        if isinstance(v, str) and f is not None and _accepts_str(f.annotation):
            keep[k] = v
        else:
            rest[k] = v
    out = _orig(self, rest) if rest else {}
    out.update(keep)
    return {k: out[k] for k in data}


def apply() -> None:
    global _applied
    if not _applied:
        fm.FuncMetadata.pre_parse_json = _pre_parse
        _applied = True
