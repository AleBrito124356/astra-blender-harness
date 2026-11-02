"""Astra as an MCP server: driven by a real MCP client over stdio, as Claude Code would."""

import asyncio
import base64
import json
import os
import sys
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from astra_blender import mcp_server
from astra_blender.config import MCPConfig
from astra_blender.demo import demo_connect
from astra_blender.live import BlenderHub


def demo_server(tmp_path):
    config = MCPConfig()
    return mcp_server.AstraServer(
        config, tmp_path, viewer_port=None, hub=BlenderHub(config, connector=demo_connect)
    )


async def test_guard_refuses_before_blender_is_touched(tmp_path):
    astra = demo_server(tmp_path)
    try:
        result = await astra.call("run_python", {"code": "import os\nos.remove('/tmp/x')"})
        assert result.isError
        assert "Astra Guard rejected" in result.content[0].text and "import of 'os'" in result.content[0].text
        assert astra.edits == 0 and astra.ready is None, "nothing was sent to Blender"
    finally:
        await astra.close()


async def test_checkpoint_labels_are_validated_and_unknown_tools_reported(tmp_path):
    astra = demo_server(tmp_path)
    try:
        assert (await astra.call("checkpoint", {"label": "../escape"})).isError
        missing = await astra.call("restore_checkpoint", {"label": "never"})
        assert missing.isError and "No checkpoint 'never'" in missing.content[0].text
        assert (await astra.call("nope", {})).isError
    finally:
        await astra.close()


def test_tool_surface_and_annotations():
    tools = {t.name: t for t in mcp_server.tool_definitions(quality_available=True)}
    assert set(tools) == {
        "blender_status",
        "inspect_scene",
        "run_python",
        "look",
        "checkpoint",
        "restore_checkpoint",
        "frame_camera",
        "assemble_parts",
        "place_on_ground",
        "keyframe_object",
        "inspect_animation",
        "quality_check",
    }
    for name in ("blender_status", "inspect_scene", "look", "inspect_animation", "quality_check"):
        assert tools[name].annotations.readOnlyHint is True
    assert tools["run_python"].annotations.destructiveHint is True
    # Internal names never leak into what the agent reads.
    assert not any("astra_" in (t.description or "") for t in tools.values())
    assert "quality_check" not in {t.name for t in mcp_server.tool_definitions(quality_available=False)}


def test_build_prompt_scales_with_quality():
    draft = mcp_server.build_prompt("a teapot", "draft")
    final = mcp_server.build_prompt("a teapot", "final", quality_available=True)
    assert "Refine" not in draft and "Refine" in final and "quality_check" in final
    assert "BRIEF: a teapot" in final


@asynccontextmanager
async def astra_client(tmp_path, blender_config):
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "ASTRA_BLENDER", "BLENDER", "TMPDIR"}}
    args = [
        "-m",
        "astra_blender",
        "mcp",
        "--no-viewer",
        "--output",
        str(tmp_path),
        "--backend",
        "headless",
    ]
    if blender_config.blender:
        args += ["--blender", blender_config.blender]
    params = StdioServerParameters(command=sys.executable, args=args, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def text(result):
    return "\n".join(block.text for block in result.content if block.type == "text")


async def test_an_agent_builds_checks_and_restores_through_mcp(tmp_path, blender_config):
    if blender_config.transport != "headless":
        pytest.skip("drives its own headless Blender")
    async with astra_client(tmp_path, blender_config) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert {"run_python", "inspect_scene", "frame_camera", "checkpoint"} <= listed

        status = json.loads(text(await client.call_tool("blender_status", {})))
        assert status["backend"] == "headless" and status["guard"] == "on"
        assert status["objects"] == 3 and status["camera"] == "Camera"

        built = await client.call_tool(
            "run_python",
            {
                "code": "import bpy\nbpy.ops.mesh.primitive_uv_sphere_add(location=(4, 0, 1))\n"
                "bpy.context.object.name = 'Planet'\nprint('made', bpy.context.object.name)",
                "intent": "add the subject",
            },
        )
        report = text(built)
        assert not built.isError, report
        assert "Output:\nmade Planet" in report
        assert "After-edit audit" in report and "no_material: Planet" in report
        assert "astra_" not in report, "internal tool names never reach the agent"

        refused = await client.call_tool("run_python", {"code": "import subprocess"})
        assert refused.isError and "Astra Guard" in text(refused)

        framed = await client.call_tool("frame_camera", {"objects": ["Planet", "Cube"]})
        assert not framed.isError, text(framed)
        scene = json.loads(text(await client.call_tool("inspect_scene", {"objects": ["Planet"]})))
        assert [o["name"] for o in scene["objects"]] == ["Planet"]
        assert scene["objects"][0]["camera"]["corners_in_frame"] == 8

        saved = await client.call_tool("checkpoint", {"label": "with-planet"})
        assert not saved.isError, text(saved)
        await client.call_tool(
            "run_python", {"code": "import bpy\nbpy.data.objects.remove(bpy.data.objects['Planet'])"}
        )
        restored = await client.call_tool("restore_checkpoint", {"label": "with-planet"})
        assert not restored.isError, text(restored)
        names = await client.call_tool(
            "run_python", {"code": "import bpy\nprint(sorted(bpy.data.objects.keys()))"}
        )
        assert "'Planet'" in text(names)

        image = await client.call_tool("look", {"max_size": 200})
        png = next(block for block in image.content if block.type == "image")
        assert base64.b64decode(png.data)[:4] == b"\x89PNG"

        prompt = await client.get_prompt("build_scene", {"brief": "a lighthouse", "quality": "draft"})
        assert "BRIEF: a lighthouse" in prompt.messages[0].content.text

    session = next(tmp_path.glob("agent-*"))
    checkpoints = sorted(p.name for p in session.glob("*.blend"))
    assert checkpoints == ["000-start.blend", "001-with-planet.blend"]
    edits = [json.loads(line) for line in (session / "edits.jsonl").read_text().splitlines()]
    assert edits[0]["intent"] == "add the subject"


async def test_the_live_viewer_shares_the_agents_blender(tmp_path):
    import httpx

    astra = demo_server(tmp_path)
    astra.viewer_port = 0
    try:
        url = await astra.start_viewer()
        async with httpx.AsyncClient(base_url=url, headers={"Host": "127.0.0.1"}) as http:
            for _ in range(50):
                try:
                    session = (await http.get("/api/session")).json()
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
            token = {"X-Astra-Token": session["token"]}
            live = (await http.get("/api/scene/live", headers=token)).json()
            for _ in range(40):
                if live["snapshot"]:
                    break
                await asyncio.sleep(0.1)
                live = (await http.get("/api/scene/live", headers=token)).json()
            assert live["snapshot"]["scene"] == "Demo fixture", live
            page = await http.get("/")
            assert page.status_code == 200 and "<html" in page.text.lower()
    finally:
        await astra.close()
    assert astra.hub.closing, "the server, not the viewer, owns the connection"
