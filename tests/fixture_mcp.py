"""Actual stdio MCP fixture. Never executes supplied code."""

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

from astra_blender.demo import illustration

app = FastMCP("Blender fixture")


@app.tool()
def get_scene_info(user_prompt: str) -> str:
    return '{"objects":[{"name":"Fixture cube","type":"MESH"}]}'


@app.tool()
def get_viewport_screenshot() -> Image:
    return Image(data=illustration(), format="png")


@app.tool()
def execute_blender_code(code: str) -> str:
    return "Fixture does not execute code"


if __name__ == "__main__":
    app.run(transport="stdio")
