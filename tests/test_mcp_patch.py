"""The mcp SDK would turn string args like '{}' into dicts and fail validation; the patch keeps string-capable params as strings."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desktop_bridge import mcp_patch  # noqa: E402


class McpPatch(unittest.TestCase):
    def test_json_looking_strings_stay_strings_and_lists_still_parse(self):
        mcp_patch.apply()
        from mcp.server.mcpserver import MCPServer

        m = MCPServer("t")

        @m.tool()
        def echo(content: str | None = None, region: list[int] | None = None, window: int | str | None = None) -> str:
            return f"content={type(content).__name__}:{content}|region={type(region).__name__}:{region}|window={type(window).__name__}:{window}"

        async def go(args):
            res = await m.call_tool("echo", args)
            return res.structured_content["result"]

        for v in ["{}", '{"a":1}', "[1,2]", "null", "true", "123", "plain"]:
            self.assertTrue(asyncio.run(go({"content": v})).startswith(f"content=str:{v}|"), v)
        self.assertIn("region=list:[1, 2, 3, 4]", asyncio.run(go({"region": "[1, 2, 3, 4]"})))  # list-typed params still accept JSON-in-a-string
        self.assertIn("window=", asyncio.run(go({"window": "5048164"})))


if __name__ == "__main__":
    unittest.main()
