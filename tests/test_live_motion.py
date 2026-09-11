"""Opt-in assembly animation test. Creates uniquely named objects; never deletes a scene."""

import json
import os
import uuid
from pathlib import Path

import pytest
from PIL import Image

from astra_blender.config import MCPConfig, RunConfig
from astra_blender.engine import Run, execute
from astra_blender.live import BlenderHub, LiveScene
from astra_blender.references import attach

pytestmark = pytest.mark.skipif(
    not os.environ.get("ASTRA_LIVE_MCP_CONFIG"), reason="Requires disposable Blender"
)


async def test_reference_assembly_animation_and_motion_framing(tmp_path):
    prefix = "ASTRA_TEST_" + uuid.uuid4().hex[:8]
    body, glass, ground, root = [prefix + n for n in ("Body", "Glass", "Ground", "Root")]
    setup = (
        "import bpy\n"
        "bpy.ops.mesh.primitive_cube_add(location=(0,0,-0.5),scale=(2,1,0.5))\n"
        "bpy.context.object.name=" + repr(body) + "\n"
        "bpy.ops.mesh.primitive_cube_add(location=(0.6,-1.04,-0.2),scale=(0.4,0.04,0.15))\n"
        "bpy.context.object.name=" + repr(glass) + "\n"
        "bpy.ops.mesh.primitive_plane_add(size=30)\nbpy.context.object.name=" + repr(ground)
    )
    operations = [
        ("execute_blender_code", {"code": setup}),
        ("astra_assemble_parts", {"root_name": root, "names": [body, glass], "anchor": body}),
        ("astra_place_on_ground", {"root_name": root, "ground_name": ground}),
        (
            "astra_keyframe_object",
            {
                "name": root,
                "fps": 24,
                "interpolation": "LINEAR",
                "keys": [
                    {"frame": 1, "location": [0, 0, 0.5], "rotation": [0, 0, 0]},
                    {"frame": 24, "location": [4, 0, 0.5], "rotation": [0, 0, 1.570796]},
                ],
            },
        ),
        ("astra_frame_camera", {"objects": [body, glass], "frames": [1, 12, 24]}),
    ]

    class Scripted:
        turn = 0

        async def complete(self, messages, tools):
            self.turn += 1
            assert any(
                isinstance(m.get("content"), list) and any(b.get("type") == "image_url" for b in m["content"])
                for m in messages
            )
            if 2 <= self.turn <= 6:
                name, args = operations[self.turn - 2]
                return {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": str(self.turn),
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }, {"total_tokens": 0}
            return {"role": "assistant", "content": "Scripted animation fixture; no paid model."}, {
                "total_tokens": 0
            }

    config = MCPConfig.model_validate_json(Path(os.environ["ASTRA_LIVE_MCP_CONFIG"]).read_text())
    hub = BlenderHub(config)
    live = LiveScene(hub)
    photo = tmp_path / "reference.png"
    Image.new("RGB", (80, 60), "orange").save(photo)
    run = Run(
        RunConfig(
            prompt="Animate a reference assembly",
            animation="on",
            animation_frames=24,
            model="test/scripted",
            quality="draft",
            auto_approve=True,
        ),
        tmp_path,
    )
    attach(run, [photo])
    try:
        await execute(run, config, Scripted(), hub.connection)
        assert run.status == "completed", run.events[-1]
        report = json.loads((run.directory / "animation.json").read_text())
        assert report["start"] == 1 and report["end"] == 24
        assert any(a["object"] == root for a in report["actions"])
        for sample in report["samples"]:
            subjects = [o for o in sample["scene"]["objects"] if o["name"] in [body, glass]]
            assert len(subjects) == 2
            assert all(o["camera"]["corners_in_frame"] == 8 for o in subjects)
            assert all(o["bounds"][0][2] >= -0.001 for o in subjects)
            assert all(o["parent"] == root for o in subjects)
        assert (run.directory / "scene.blend").is_file()
        assert (run.directory / "reference-1.jpg").is_file()
        scene = await live.fresh()
        assert root in scene["snapshot"]["timeline"]["animated_objects"]
    finally:
        await live.close()
        await hub.close()
