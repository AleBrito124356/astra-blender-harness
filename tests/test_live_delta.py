import ast
import json

from mcp.types import CallToolResult, TextContent

from astra_blender import spatial
from astra_blender.live import LiveScene


def entry(key, hash_value, x, geometry=True, unchanged=False):
    item = {
        "id": "0:" + key,
        "key": key,
        "name": key,
        "type": "MESH",
        "bounds": [[x, 0, 0], [x + 1, 1, 1]],
        "dimensions": [1, 1, 1],
        "materials": [
            {"name": "Clay", "color": [0.5, 0.5, 0.5], "roughness": 0.5, "metallic": 0, "opacity": 1}
        ],
        "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, 0, 0, 1],
        "hash": hash_value,
    }
    if unchanged:
        item["unchanged"] = True
    elif geometry:
        item.update(positions=[0, 0, 0, 1, 0, 0, 0, 1, 0], triangles=[0, 1, 2], material_indices=[0])
    return item


def probe(meshes):
    return {
        "schema_version": 1,
        "scene": "Scene",
        "frame": 1,
        "timeline": {"start": 1, "end": 24, "fps": 24, "animated_objects": [], "fingerprint": ""},
        "objects": [
            {k: v for k, v in m.items() if k not in ("positions", "triangles", "material_indices")}
            for m in meshes
        ],
        "meshes": meshes,
        "camera": None,
        "warnings": [],
        "render": {"resolution": [1920, 1080], "pixel_aspect": [1, 1]},
        "capabilities": {},
    }


class ScriptedHub:
    """Serves two probes: a full one, then one where A moved and B changed shape."""

    operation = None

    def __init__(self):
        self.codes = []
        self.responses = [
            probe([entry("A", "hashA", 0), entry("B", "hashB", 3)]),
            probe([entry("A", "hashA", 2, unchanged=True), entry("B", "hashB2", 3)]),
        ]

    async def call_tool(self, name, arguments):
        self.codes.append(arguments["code"])
        data = self.responses.pop(0)
        return CallToolResult(content=[TextContent(type="text", text="ASTRA_SCENE_JSON:" + json.dumps(data))])


def test_probe_code_carries_the_known_hashes_and_parses():
    code = spatial.probe_code(geometry=True, known={"A": "hashA"})
    ast.parse(code)
    assert "known={'A': 'hashA'}" in code
    assert spatial.known_hashes(probe([entry("A", "hashA", 0), entry("B", None, 1)])) == {"A": "hashA"}


async def test_unchanged_meshes_keep_their_geometry_across_refreshes():
    hub = ScriptedHub()
    live = LiveScene(hub)
    first = await live.fresh()
    assert first["snapshot"]["meshes"][0]["positions"]
    second = await live.fresh()
    # Blender was told what the server already held ...
    assert "known={'A': 'hashA', 'B': 'hashB'}" in hub.codes[1]
    # ... and the merged snapshot is complete: A keeps its old geometry at its
    # new position, B carries the geometry it was re-sent with.
    a, b = second["snapshot"]["meshes"]
    assert "unchanged" not in a
    assert a["positions"] == [0, 0, 0, 1, 0, 0, 0, 1, 0] and a["matrix"][12] == 2
    assert b["hash"] == "hashB2" and b["positions"]
    assert second["revision"] != first["revision"]


def test_a_mesh_the_server_no_longer_holds_becomes_a_proxy():
    merged = spatial.merge_unchanged(probe([entry("Z", "h", 0, unchanged=True)]), previous=None)
    assert merged["meshes"][0]["proxy"] is True
    assert "unchanged" not in merged["meshes"][0]
