import ast
import json

from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent

from astra_blender import spatial
from astra_blender.live import LiveScene
from astra_blender.server import create_app

BAKE = {
    "schema_version": 1,
    "start": 1,
    "end": 48,
    "fps": 24.0,
    "step": 1,
    "frames": list(range(1, 49)),
    "tracks": {"Root": [[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, i / 10, 0, 0, 1] for i in range(48)]},
    "deforming": ["Cloth"],
    "restored_frame": 7,
}


class FakeHub:
    """Returns a canned bake and records the code Blender would have run."""

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return CallToolResult(content=[TextContent(type="text", text="ASTRA_SCENE_JSON:" + json.dumps(BAKE))])


def test_generated_blender_scripts_parse():
    # Every trusted script is assembled from files plus a print line; a typo in
    # any of them would only surface inside Blender.
    for code in (
        spatial.probe_code(),
        spatial.probe_code(geometry=True),
        spatial.bake_code({"step": 2}),
        spatial.tool_code("astra_inspect_animation", {}),
        spatial.tool_code("astra_frame_camera", {"objects": ["A"]}),
    ):
        ast.parse(code)
    assert "astra_motion_bake(step=2)" in spatial.bake_code({"step": 2})


async def test_bake_reads_matrices_once_through_the_hub():
    hub = FakeHub()
    data = await LiveScene(hub).bake(step=3)
    assert data["tracks"]["Root"][47][12] == 4.7
    assert data["deforming"] == ["Cloth"]
    assert len(hub.calls) == 1
    name, arguments = hub.calls[0]
    assert name == "execute_blender_code"
    assert "astra_motion_bake(step=3)" in arguments["code"]
    # The bake restores the playhead itself; nothing else moves it.
    assert "scene.frame_set(original)" in arguments["code"]


def test_bake_endpoint_requires_auth_and_bounds_the_step(tmp_path, monkeypatch):
    async def fake_bake(self, step=1):
        return {**BAKE, "step": step}

    monkeypatch.setattr(LiveScene, "bake", fake_bake)
    with TestClient(create_app(output=tmp_path), base_url="http://127.0.0.1") as client:
        assert client.post("/api/scene/bake", json={"step": 1}).status_code == 401
        session = client.get("/api/session").json()
        assert "motion_bake" in session["features"]
        headers = {"X-Astra-Token": session["token"]}
        assert client.post("/api/scene/bake", headers=headers, json={"step": 0}).status_code == 422
        assert client.post("/api/scene/bake", headers=headers, json={"frames": 3}).status_code == 422
        baked = client.post("/api/scene/bake", headers=headers, json={"step": 2}).json()
        assert baked["step"] == 2 and baked["tracks"]["Root"]
