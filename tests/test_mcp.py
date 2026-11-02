import sys
from pathlib import Path

from astra_blender.bridge import connect, discover
from astra_blender.config import MCPConfig


def fixture(name):
    return MCPConfig(transport="stdio", command=sys.executable, args=[str(Path(__file__).with_name(name))])


async def test_real_stdio_handshake_discovery_and_image():
    config = fixture("fixture_mcp.py")
    async with connect(config) as session:
        tools = await discover(session, config.allowed_tools)
        assert tools["execute_blender_code"].inputSchema["required"] == ["code"]
        result = await session.call_tool("get_viewport_screenshot", {})
        assert result.content[0].type == "image"
        scene = await session.call_tool("get_scene_info", {"user_prompt": "Test scene"})
        assert "Fixture cube" in scene.content[0].text


async def test_blender_mcp_2_look_stands_in_for_the_viewport_screenshot():
    # Upstream 2.x has no get_viewport_screenshot; discovery used to fail with
    # "missing required tools" against the add-on users install today.
    config = fixture("fixture_mcp_v2.py")
    async with connect(config) as session:
        tools = await discover(session, config.allowed_tools)
        assert set(tools) >= {"get_scene_info", "execute_blender_code", "get_viewport_screenshot"}
        result = await session.call_tool("get_viewport_screenshot", {"max_size": 640})
        assert result.content[0].type == "image"
        assert "look mode=viewport max_size=640" in result.content[1].text
