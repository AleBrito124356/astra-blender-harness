"""Opt-in integration: modifies only a disposable Blender session selected by env."""

import json
import os
import uuid
from pathlib import Path

import pytest

from astra_blender.config import MCPConfig, RunConfig
from astra_blender.engine import Run, execute
from astra_blender.live import BlenderHub, LiveScene


@pytest.mark.skipif(
    not os.environ.get("ASTRA_LIVE_MCP_CONFIG"), reason="Requires a disposable live Blender MCP session"
)
@pytest.mark.parametrize("vision", [False, True])
async def test_live_blender_scene_copy_and_viewport(tmp_path, vision):
    name = "ASTRA_LIVE_TEST_" + uuid.uuid4().hex[:8]

    class Scripted:
        turn = 0

        async def complete(self, messages, tools):
            if not vision:
                assert "image_url" not in json.dumps(messages)
                assert all(t["function"]["name"] != "get_viewport_screenshot" for t in tools)
            self.turn += 1
            if self.turn in {2, 3}:
                tool = "execute_blender_code" if self.turn == 2 else "astra_frame_camera"
                arguments = (
                    {
                        "code": "import bpy\nbpy.ops.mesh.primitive_torus_add(location=(0,0,2))\nbpy.context.object.name="
                        + repr(name)
                    }
                    if self.turn == 2
                    else {"objects": [name]}
                )
                return {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "smoke-" + str(self.turn),
                            "type": "function",
                            "function": {"name": tool, "arguments": json.dumps(arguments)},
                        }
                    ],
                }, {"total_tokens": 0}
            return {"role": "assistant", "content": "Scripted test phase finished; no LLM called."}, {
                "total_tokens": 0
            }

    config = MCPConfig.model_validate_json(
        Path(os.environ["ASTRA_LIVE_MCP_CONFIG"]).read_text(encoding="utf-8")
    )
    hub, live = BlenderHub(config), None
    try:
        live = LiveScene(hub)
        await live.read()
        await live.refresh_task
        assert live.snapshot, live.error
        before = live.revision
        run = Run(
            RunConfig(
                prompt="Create and frame a torus for an integration test.",
                model="test/scripted",
                auto_approve=True,
                quality="draft",
                vision=vision,
            ),
            tmp_path,
        )
        await execute(run, config, provider=Scripted(), connector=hub.connection)
        assert run.status == "completed", run.events[-1]
        assert (run.directory / "scene.blend").is_file()
        assert bool(list(run.directory.glob("viewport-*.png"))) is vision
        quality = json.loads((run.directory / "quality.json").read_text())
        assert next(o for o in quality["objects"] if o["name"] == name)["camera"]["corners_in_frame"] == 8
        live.attempted_at = 0
        await live.read(before)
        await live.refresh_task
        assert live.revision != before
        mesh = next(m for m in live.snapshot["meshes"] if m["name"] == name)
        assert len(mesh["normals"]) == len(mesh["triangles"]) * 3
    finally:
        if live:
            await live.close()
        await hub.close()
