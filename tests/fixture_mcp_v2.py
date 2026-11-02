"""Actual stdio MCP fixture shaped like upstream Blender MCP 2.x: `look`
replaces `get_viewport_screenshot`. Never executes supplied code."""

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

from astra_blender.demo import illustration

app = FastMCP("Blender fixture v2")
calls = []


@app.tool()
def get_scene_info(user_prompt: str = "", query: str | None = None, limit: int = 20) -> str:
    return "Scene: 1 object\nFixture cube | MESH"


@app.tool()
def look(mode: str | None = None, max_size: int = 768, user_prompt: str = "") -> list:
    return [Image(data=illustration(), format="png"), f"look mode={mode} max_size={max_size}"]


@app.tool()
def execute_blender_code(code: str, user_prompt: str = "") -> str:
    return "Fixture does not execute code"


if __name__ == "__main__":
    app.run(transport="stdio")
