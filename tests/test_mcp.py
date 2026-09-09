import sys
from pathlib import Path

from astra_blender.bridge import connect, discover
from astra_blender.config import MCPConfig


async def test_real_stdio_handshake_discovery_and_image():
    config = MCPConfig(command=sys.executable, args=[str(Path(__file__).with_name("fixture_mcp.py"))])
    async with connect(config) as session:
        tools = await discover(session, config.allowed_tools)
        assert tools["execute_blender_code"].inputSchema["required"] == ["code"]
        result = await session.call_tool("get_viewport_screenshot", {})
        assert result.content[0].type == "image"
        scene = await session.call_tool("get_scene_info", {"user_prompt": "Test scene"})
        assert "Fixture cube" in scene.content[0].text
