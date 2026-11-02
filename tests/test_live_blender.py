"""Opt-in integration: modifies only a disposable Blender session selected by env."""

import json
import uuid

import pytest

from astra_blender.config import RunConfig
from astra_blender.engine import Run, execute
from astra_blender.live import BlenderHub, LiveScene


@pytest.mark.parametrize("vision", [False, True])
async def test_live_blender_scene_copy_and_viewport(tmp_path, vision, blender_config):
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

    config = blender_config
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


async def test_continuing_a_headless_run_reopens_its_scene_in_a_new_blender(tmp_path, blender_config):
    """A headless Blender ends with its run. Continuing later, in a new Blender
    process, must start from the scene the first run built - not a factory cube."""
    if blender_config.transport != "headless":
        pytest.skip("headless-specific")
    from astra_blender.bridge import connect

    name = "ASTRA_RESUME_" + uuid.uuid4().hex[:8]

    class BuildThenFail:
        turn = 0

        async def complete(self, messages, tools):
            self.turn += 1
            if self.turn == 2:
                code = f"import bpy\nbpy.ops.mesh.primitive_torus_add()\nbpy.context.object.name = {name!r}"
                return {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "build",
                            "type": "function",
                            "function": {
                                "name": "execute_blender_code",
                                "arguments": json.dumps({"code": code}),
                            },
                        }
                    ],
                }, {"total_tokens": 0}
            if self.turn == 3:
                raise RuntimeError("provider went away mid-build")
            return {"role": "assistant", "content": "Plan: one torus."}, {"total_tokens": 0}

    settings = dict(
        prompt="Build a torus, then continue.", model="test/scripted", auto_approve=True, quality="draft"
    )
    first = Run(RunConfig(**settings), tmp_path)
    await execute(first, blender_config, provider=BuildThenFail(), connector=connect)
    assert first.status == "failed"
    assert (first.directory / "stopped.blend").is_file(), [e["type"] for e in first.events]
    state = json.loads((first.directory / "state.json").read_text())

    class Inspect:
        async def complete(self, messages, tools):
            return {"role": "assistant", "content": "Continued."}, {"total_tokens": 0}

    second = Run(RunConfig(**settings, resume_from=first.id), tmp_path)
    await execute(second, blender_config, provider=Inspect(), connector=connect, state=state)
    assert second.status == "completed", second.events[-1]
    restored = [e for e in second.events if e["type"] == "restored"]
    assert restored and restored[0]["file"] == "stopped.blend"
    quality = json.loads((second.directory / "quality.json").read_text())
    assert name in [o["name"] for o in quality["objects"]]
