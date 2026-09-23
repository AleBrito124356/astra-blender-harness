"""The headless backend drives real Blender 5.2 and answers like upstream blender-mcp."""

import json
import time

import pytest
from conftest import ScriptedProvider, action

from astra_blender import registry, scripts, spatial
from astra_blender.config import MCPConfig, RunConfig
from astra_blender.engine import Run, execute
from astra_blender.headless import HeadlessBlender, headless_connect, upstream_tools

pytestmark = pytest.mark.blender


def test_the_session_lists_the_upstream_tools_exactly():
    tools = {tool.name: tool for tool in upstream_tools()}
    assert list(tools) == ["get_scene_info", "get_object_info", "get_viewport_screenshot", "execute_blender_code"]
    assert tools["execute_blender_code"].inputSchema["required"] == ["code"]
    assert "user_prompt" in tools["get_scene_info"].inputSchema["required"]


def test_round_trip_returns_upstream_strings(headless):
    assert headless.version.startswith("5.2")
    assert headless.execute_blender_code("import bpy\nprint(len(bpy.data.objects))") == (
        "Code executed successfully: 3\n"
    )
    assert headless.execute_blender_code("print('partial')\nraise ValueError('boom')") == (
        "Error executing code: Communication error with Blender: Code execution error: boom"
    )
    info = json.loads(headless.get_scene_info())
    assert info == {
        "name": "Scene",
        "object_count": 3,
        "objects": [
            {"name": "Cube", "type": "MESH", "location": [0.0, 0.0, 0.0]},
            {"name": "Light", "type": "LIGHT", "location": [4.08, 1.01, 5.9]},
            {"name": "Camera", "type": "CAMERA", "location": [7.36, -6.93, 4.96]},
        ],
        "materials_count": 2,
    }
    cube = json.loads(headless.get_object_info("Cube"))
    assert cube["world_bounding_box"] == [[-1.0, -1.0, -1.0], [1.0, 1.0, 1.0]]
    assert cube["mesh"] == {"vertices": 8, "edges": 12, "polygons": 6} and cube["materials"] == ["Material"]
    assert headless.get_object_info("Nope") == (
        "Error getting object info: Communication error with Blender: Object not found: Nope"
    )
    assert headless.get_viewport_screenshot().startswith(
        "Error executing tool get_viewport_screenshot: Screenshot failed: "
    )


def test_scene_info_lists_only_the_first_ten_objects(headless):
    headless.execute_blender_code(
        "import bpy\nfor i in range(12):\n    bpy.context.scene.collection.objects.link(bpy.data.objects.new('E' + str(i), None))"
    )
    info = json.loads(headless.get_scene_info())
    assert info["object_count"] == 15 and len(info["objects"]) == 10


def test_a_rejected_script_never_runs(headless):
    code = "import bpy\nbpy.context.scene.collection.objects.link(bpy.data.objects.new('Leak', None))\nimport os"
    text = headless.execute_blender_code(code)
    assert text.startswith("Rejected by safe mode - line 3: import of 'os' is not allowed\n\n")
    assert "BLENDER_MCP_SAFE_MODE is enabled" in text
    assert "Leak" not in headless.execute_blender_code("import bpy\nprint(sorted(o.name for o in bpy.data.objects))")


def test_reset_returns_to_the_factory_scene(headless):
    headless.execute_blender_code("import bpy\nbpy.data.objects.remove(bpy.data.objects['Cube'])")
    assert headless.reset() == {"objects": ["Camera", "Cube", "Light"]}


def test_a_stuck_script_times_out_and_the_worker_restarts(blender_exe):
    with HeadlessBlender(exe=blender_exe, timeout=60) as blender:
        blender.execute_blender_code("import bpy\nbpy.data.objects['Cube'].name = 'Renamed'")
        started = time.perf_counter()
        text = blender.execute_blender_code("x = 0\nwhile True:\n    x += 1\n", timeout=3)
        assert time.perf_counter() - started < 30
        assert text.startswith("Error executing code: Timeout waiting for Blender response")
        assert blender.restarts == 1
        # A fresh worker: the factory scene again, and it answers normally.
        assert blender.execute_blender_code("import bpy\nprint(sorted(o.name for o in bpy.data.objects))") == (
            "Code executed successfully: ['Camera', 'Cube', 'Light']\n"
        )


@pytest.mark.parametrize(
    "name", ["astra_inspect_scene", "astra_frame_camera", "astra_assemble_parts", "astra_inspect_animation"]
)
def test_ported_tools_run_in_blender_and_parse(headless, name):
    headless.execute_blender_code(
        "import bpy\nbpy.context.scene.collection.objects.link(bpy.data.objects.new('Wheel', bpy.data.meshes['Cube']))"
    )
    args = {
        "astra_inspect_scene": {},
        "astra_frame_camera": {"objects": ["Cube", "Wheel"], "margin": 0.15},
        "astra_assemble_parts": {"root_name": "Rig", "names": ["Cube", "Wheel"], "anchor": "Cube"},
        "astra_inspect_animation": {"frames": [1, 60]},
    }[name]
    data = scripts.parse(headless.execute_blender_code(scripts.build(registry.get(name), args)))
    if name == "astra_frame_camera":
        assert data["camera"] == "Camera" and data["subjects"] == ["Cube", "Wheel"]
    elif name == "astra_assemble_parts":
        assert data["root"] == "Rig"
    else:
        assert data["schema_version"] == 1


def test_the_ported_probe_equals_the_live_viewer_probe(headless):
    via_registry = scripts.parse(headless.execute_blender_code(scripts.build(registry.get("astra_inspect_scene"), {})))
    via_viewer = scripts.parse(headless.execute_blender_code(spatial.probe_code()))
    assert via_registry == via_viewer
    assert [o["name"] for o in via_registry["objects"]] == ["Cube"]


async def test_the_connector_speaks_mcp_to_the_engine(headless_blender):
    async with headless_connect(MCPConfig(), blender=headless_blender, reset=True) as session:
        page = await session.list_tools()
        assert [tool.name for tool in page.tools][-1] == "execute_blender_code" and page.nextCursor is None
        result = await session.call_tool("execute_blender_code", {"code": "print('hi')", "user_prompt": "x"})
        assert result.content[0].text == "Code executed successfully: hi\n" and not result.isError
        shot = await session.call_tool("get_viewport_screenshot", {"max_size": 800})
        assert shot.isError


def build_scene():
    return (
        "import bpy\n"
        "scene = bpy.context.scene\n"
        "ground = bpy.data.meshes.new('GroundMesh')\n"
        "ground.from_pydata([(-20, -20, 0), (20, -20, 0), (20, 20, 0), (-20, 20, 0)], [], [(0, 1, 2, 3)])\n"
        "scene.collection.objects.link(bpy.data.objects.new('Ground', ground))\n"
        "body = bpy.data.objects.new('CarBody', bpy.data.meshes['Cube'].copy())\n"
        "body.location = (0, 0, 3)\n"
        "body.scale = (2, 1, 0.5)\n"
        "scene.collection.objects.link(body)\n"
        "wheel = bpy.data.objects.new('Wheel', bpy.data.meshes['Cube'].copy())\n"
        "wheel.location = (1.5, -1.1, 2.4)\n"
        "wheel.scale = (0.4, 0.1, 0.4)\n"
        "scene.collection.objects.link(wheel)\n"
        "bpy.data.objects.remove(bpy.data.objects['Cube'])\n"
        "print('built')"
    )


async def test_a_scripted_0_3_2_run_completes_over_headless_blender(tmp_path, headless_blender):
    turns = [
        action("astra_inspect_scene", {}),
        {"role": "assistant", "content": "Plan: a car on the ground, driving right."},
        action("execute_blender_code", {"code": build_scene()}),
        action(
            "astra_assemble_parts",
            {"root_name": "CarRoot", "names": ["CarBody", "Wheel"], "anchor": "CarBody", "max_gap": 0.3},
        ),
        action("astra_place_on_ground", {"root_name": "CarRoot", "ground_name": "Ground"}),
        action(
            "astra_keyframe_object",
            {
                "name": "CarRoot",
                "fps": 24,
                "interpolation": "LINEAR",
                "keys": [{"frame": 1, "location": [0, 0, 0]}, {"frame": 48, "location": [6, 0, 0]}],
            },
        ),
        action("astra_frame_camera", {"objects": ["CarBody", "Wheel"], "frames": [1, 24, 48]}),
        {"role": "assistant", "content": "Built and animated."},
        action("astra_inspect_animation", {"frames": [1, 24, 48]}),
        {"role": "assistant", "content": "Review: the car rests on the ground and stays in frame."},
    ]
    provider = ScriptedProvider(turns)
    run = Run(
        RunConfig(prompt="Animate a car driving on the ground", auto_approve=True, vision=False, animation="on"),
        tmp_path,
    )

    def connector(config):
        return headless_connect(config, blender=headless_blender, reset=True)

    await execute(run, MCPConfig(), provider, connector)
    assert run.status == "completed", [e for e in run.events if e["type"] in {"failed", "tool_result"}][-3:]
    results = {e["tool"]: e for e in run.events if e["type"] == "tool_result"}
    for name in ["astra_assemble_parts", "astra_place_on_ground", "astra_keyframe_object", "astra_frame_camera"]:
        assert not results[name]["is_error"], results[name]["text"]
    framed = scripts.parse(results["astra_frame_camera"]["text"])
    assert framed["subjects"] == ["CarBody", "Wheel"]
    grounded = scripts.parse(results["astra_place_on_ground"]["text"])
    assert grounded["offset_z"] == pytest.approx(-2.0, abs=1e-4)
    animation = json.loads((run.directory / "animation.json").read_text(encoding="utf-8"))
    root = next(a for a in animation["actions"] if a["object"] == "CarRoot")
    assert root["changing_channels"] == ["location"]
    quality = json.loads((run.directory / "quality.json").read_text(encoding="utf-8"))
    assert {o["name"] for o in quality["objects"]} == {"Ground", "CarBody", "Wheel"}
    for name in ["checkpoint.blend", "build.blend", "refine.blend", "scene.blend"]:
        # Blender 5 compresses .blend files with zstd by default.
        head = (run.directory / name).read_bytes()[:7]
        assert head == b"BLENDER" or head[:4] == bytes.fromhex("28b52ffd"), (name, head)
    # The engine's own after-edit audits ran in Blender too.
    assert any("After-edit spatial checks" in str(m.get("content", "")) for m in run.messages)
    # The saved scene opens and holds the animated assembly (test-only: opening
    # a .blend is outside safe mode, so this goes to the worker directly).
    path = (run.directory / "scene.blend").as_posix()
    reopen = "\n".join(
        [
            "import bpy",
            f"bpy.ops.wm.open_mainfile(filepath={path!r})",
            "root = bpy.data.objects['CarRoot']",
            "print(sorted(o.name for o in bpy.data.objects), root.animation_data.action is not None,"
            " bpy.context.scene.frame_end)",
        ]
    )
    reopened = headless_blender.request("exec", code=reopen)
    assert reopened["ok"], reopened
    assert reopened["stdout"].strip() == "['Camera', 'CarBody', 'CarRoot', 'Ground', 'Light', 'Wheel'] True 48"


async def test_headless_connect_is_a_drop_in_engine_connector(tmp_path, blender_exe):
    # The documented one-liner: no shared worker, so the connector starts its
    # own background Blender and closes it when the run ends.
    run = Run(RunConfig(prompt="Create a lamp", auto_approve=True, vision=False), tmp_path)
    await execute(run, MCPConfig(), ScriptedProvider(), connector=headless_connect)
    assert run.status == "completed"
    assert (run.directory / "scene.blend").stat().st_size > 10_000
    evidence = [e for e in run.events if e["type"] == "tool_result" and e["tool"] == "get_scene_info"]
    assert json.loads(evidence[0]["text"])["objects"][0]["name"] == "Cube"
