"""common.py and measure.py measured inside real headless Blender 5.2.

Each test builds a small scene with safe-mode-clean bpy, then runs a probe
made of common.py + measure.py + a few lines that print the marker line, so
the helpers are exercised exactly as a trusted tool would call them.
"""

import json
import math

import pytest

from astra_blender import scripts

pytestmark = pytest.mark.blender

HELPERS = scripts.source("common.py") + "\n" + scripts.source("measure.py") + "\n"


def run(headless, body):
    """Run common + measure + body; body assigns `result`, which is printed as JSON."""
    code = HELPERS + body + "\nimport json\nprint('ASTRA_SCENE_JSON:' + json.dumps(result))\n"
    text = headless.execute_blender_code(code)
    assert text.startswith("Code executed successfully:"), text[:2000]
    return scripts.parse(text)


def setup(headless, code):
    text = headless.execute_blender_code("import bpy\nimport math\nfrom mathutils import Vector\n" + code)
    assert text.startswith("Code executed successfully:"), text[:2000]


def test_clock_budget_require_and_tags(headless):
    data = run(
        headless,
        """
t0 = astra_clock()
total = 0
for i in range(200000):
    total += i
elapsed = astra_clock() - t0
left = astra_budget_left(t0, 60.0)
try:
    astra_require(["Cube", "Nope", "Missing"])
    message = None
except ValueError as error:
    message = str(error)
cube = astra_require(["Cube"])[0]
before = astra_is_astra(cube)
cube["astra_kind"] = "box"
result = {"elapsed": elapsed, "left": left, "message": message, "before": before,
          "after": astra_is_astra(cube), "cube": cube.name}
""",
    )
    assert 0 < data["elapsed"] < 5
    assert 55 < data["left"] <= 60
    assert data["message"] == "Unknown objects: Nope, Missing"
    assert (data["before"], data["after"], data["cube"]) == (False, True, "Cube")


def test_update_refreshes_matrix_world_and_world_box_follows_children(headless):
    setup(
        headless,
        """
root = bpy.data.objects.new("Root", None)
bpy.context.scene.collection.objects.link(root)
cube = bpy.data.objects["Cube"]
cube.parent = root
""",
    )
    data = run(
        headless,
        """
root = bpy.data.objects["Root"]
root.location.x += 5.0
stale = root.matrix_world.translation.x
astra_update()
fresh = root.matrix_world.translation.x
lo, hi = astra_world_box([root])
result = {"stale": stale, "fresh": fresh, "lo": list(lo), "hi": list(hi)}
""",
    )
    assert data["stale"] == 0.0 and data["fresh"] == 5.0
    assert data["lo"] == pytest.approx([4, -1, -1]) and data["hi"] == pytest.approx([6, 1, 1])


def test_fcurves_on_layered_actions(headless):
    data = run(
        headless,
        """
cube = bpy.data.objects["Cube"]
empty = astra_fcurves(cube)
curves = astra_fcurves(cube, ensure=True)
curve = curves.new("location", index=2)
curve.keyframe_points.add(2)
curve.keyframe_points.foreach_set("co", [1.0, 0.0, 25.0, 3.0])
curve.update()
bpy.context.scene.frame_set(25)
action = cube.animation_data.action
again = len(astra_fcurves(cube))
cube.keyframe_insert(data_path="location", index=0, frame=10)
result = {"empty": len(empty), "z_at_25": cube.matrix_world.translation.z,
          "legacy_fcurves": hasattr(action, "fcurves"), "layers": len(action.layers),
          "paths": sorted([c.data_path + str(c.array_index) for c in astra_fcurves(cube)]),
          "same": again}
""",
    )
    assert data["empty"] == 0
    assert data["z_at_25"] == pytest.approx(3.0)
    assert data["legacy_fcurves"] is False and data["layers"] == 1
    # keyframe_insert and the helper share one channelbag.
    assert data["paths"] == ["location0", "location2"] and data["same"] == 1


def test_geometry_counts_converted_objects_once_and_aggregates_instances(headless):
    setup(
        headless,
        """
scene = bpy.context.scene
text = bpy.data.curves.new("TitleText", "FONT")
text.body = "Astra"
title = bpy.data.objects.new("Title", text)
scene.collection.objects.link(title)
curve = bpy.data.curves.new("CableCurve", "CURVE")
curve.dimensions = "3D"
curve.bevel_depth = 0.05
spline = curve.splines.new("POLY")
spline.points.add(1)
spline.points[0].co = (0.0, 3.0, 0.0, 1.0)
spline.points[1].co = (2.0, 3.0, 0.0, 1.0)
cable = bpy.data.objects.new("Cable", curve)
scene.collection.objects.link(cable)
meta = bpy.data.metaballs.new("BlobMeta")
meta.elements.new()
blob = bpy.data.objects.new("Blob", meta)
blob.location = (-5.0, 0.0, 0.0)
scene.collection.objects.link(blob)
props = bpy.data.collections.new("Props")
crate = bpy.data.objects.new("Crate", bpy.data.meshes["Cube"].copy())
props.objects.link(crate)
for index in range(2):
    holder = bpy.data.objects.new("Holder" + str(index), None)
    holder.instance_type = "COLLECTION"
    holder.instance_collection = props
    holder.location = (10.0 + 4.0 * index, 0.0, 0.0)
    scene.collection.objects.link(holder)
""",
    )
    data = run(
        headless,
        """
records = astra_geometry(bpy.context.evaluated_depsgraph_get())
result = {"records": [
    {"name": r["name"], "type": r["type"], "kind": r["kind"], "instances": r["instances"],
     "sources": r["sources"], "lo": list(r["lo"]), "hi": list(r["hi"]), "own": r["matrix"] is not None}
    for r in records]}
""",
    )["records"]
    by_name = {r["name"]: r for r in data}
    names = [r["name"] for r in data]
    assert sorted(names) == ["Blob", "Cable", "Cube", "Holder0", "Holder1", "Title"]
    assert len(names) == len(set(names))
    assert (by_name["Title"]["type"], by_name["Cable"]["type"], by_name["Blob"]["type"]) == ("FONT", "CURVE", "META")
    # The cable is measured through its evaluated mesh, not its control points.
    assert by_name["Cable"]["hi"][2] - by_name["Cable"]["lo"][2] == pytest.approx(0.1, abs=0.01)
    holder = by_name["Holder1"]
    assert (holder["kind"], holder["instances"], holder["sources"], holder["own"]) == ("instancer", 1, ["Crate"], False)
    assert holder["lo"][0] == pytest.approx(13.0) and holder["hi"][0] == pytest.approx(15.0)


def test_render_snapshot_restores_everything_even_after_a_crash(headless):
    first = run(
        headless,
        """
scene = bpy.context.scene
before = astra_render_snapshot(scene)
stored = scene.get("astra_restore") is not None
render = scene.render
render.engine = "BLENDER_WORKBENCH"
render.resolution_x, render.resolution_y, render.resolution_percentage = 160, 90, 100
render.image_settings.media_type = "IMAGE"
render.image_settings.file_format = "OPEN_EXR"
render.film_transparent = True
scene.view_settings.view_transform = "Standard"
scene.view_settings.exposure = 2.5
result = {"before": before, "stored": stored}
""",
    )
    assert first["stored"] is True
    # Nothing restored it: the next script recovers from scene['astra_restore'].
    second = run(
        headless,
        """
scene = bpy.context.scene
failed = astra_render_restore(scene)
after = dict(astra_render_snapshot(scene))
del scene["astra_restore"]
result = {"failed": failed, "after": after, "left": scene.get("astra_restore") is not None}
""",
    )
    assert second["failed"] == []
    assert second["after"] == first["before"]
    assert second["left"] is False
    assert first["before"]["engine"] == "BLENDER_EEVEE" and first["before"]["view_transform"] == "AgX"


def test_colour_conversions_round_trip(headless):
    data = run(
        headless,
        """
result = {"linear": list(astra_hex_to_linear("#808080")), "hex": astra_linear_to_hex((0.2158605, 0.2158605, 0.2158605, 1.0)),
          "red": astra_linear_to_hex(astra_hex_to_linear("#ff0000")), "clamp": astra_linear_to_hex((2.0, -1.0, 0.5))}
try:
    astra_hex_to_linear("red")
except ValueError as error:
    result["error"] = str(error)
""",
    )
    assert data["linear"] == pytest.approx([0.2158605] * 3, abs=1e-6)
    assert data["hex"] == "#808080" and data["red"] == "#ff0000" and data["clamp"] == "#ff00bc"
    assert "#rrggbb" in data["error"]


def test_projection_matches_the_view_frame_method(headless):
    data = run(
        headless,
        HELPERS_PROJECTION
        + """
scene = bpy.context.scene
camera = scene.camera
matrix = astra_camera_matrix(scene, camera)
points = [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (-1.0, 0.5, -1.0), (3.0, -2.0, 0.5)]
pairs = []
for point in points:
    u, v, behind = astra_project_uv(matrix, point)
    reference = astra_project(scene, camera, Vector(point))
    pairs.append([u, v, behind, reference.x, reference.y, reference.z])
behind_camera = astra_project_uv(matrix, tuple(camera.matrix_world.translation + (camera.matrix_world.translation - Vector((0.0, 0.0, 0.0)))))
result = {"pairs": pairs, "behind": behind_camera[2]}
""",
    )
    for u, v, behind, ref_u, ref_v, depth in data["pairs"]:
        assert behind is False and depth > 0
        assert u == pytest.approx(ref_u, abs=1e-4) and v == pytest.approx(ref_v, abs=1e-4)
    assert data["behind"] is True


HELPERS_PROJECTION = "from mathutils import Vector\n" + scripts.source("projection.py") + "\n"


def test_ray_grid_sees_coverage_and_occlusion(headless):
    setup(
        headless,
        """
scene = bpy.context.scene
camera = scene.camera
direction = (camera.matrix_world.translation - Vector((0.0, 0.0, 0.0))).normalized()
mesh = bpy.data.meshes["Cube"]
front = bpy.data.objects.new("Blocker", mesh)
front.location = direction * 4.0
front.scale = (0.6, 0.6, 0.6)
scene.collection.objects.link(front)
""",
    )
    data = run(
        headless,
        """
grid = astra_ray_grid(64, 36, layers=2)
objects = grid["objects"]
result = {"rows": len(grid["rows"]), "cols": len(grid["rows"][0]), "legend": grid["legend"],
          "void": grid["void"], "cube_first": objects["Cube"]["first"], "cube_any": objects["Cube"]["any"],
          "cube_by": objects["Cube"]["by"], "blocker_first": objects["Blocker"]["first"],
          "hits": len(grid["hits"]), "truncated": grid["truncated"], "rays": grid["rays"]}
""",
    )
    assert (data["rows"], data["cols"]) == (36, 64)
    assert set(data["legend"]) == {"Cube", "Blocker"}
    assert 0.5 < data["void"] < 0.99
    # The blocker hides part of the cube: the cube is seen behind it.
    assert data["cube_by"].get("Blocker", 0) > 0
    assert data["cube_any"] > data["cube_first"] > 0
    assert data["hits"] == data["cube_first"] + data["blocker_first"]
    assert data["truncated"] is False and data["rays"] >= data["hits"]


def test_blind_meter_reproduces_the_calibrated_constants(headless):
    setup(
        headless,
        """
scene = bpy.context.scene
for obj in list(scene.objects):
    if obj.type in {"LIGHT", "MESH"}:
        bpy.data.objects.remove(obj)
scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.0
mesh = bpy.data.meshes.new("Card")
mesh.from_pydata([(-0.1, -0.1, 0.0), (0.1, -0.1, 0.0), (0.1, 0.1, 0.0), (-0.1, 0.1, 0.0)], [], [(0, 1, 2, 3)])
card = bpy.data.objects.new("Card", mesh)
scene.collection.objects.link(card)
sun = bpy.data.lights.new("Sun", "SUN")
sun.energy = math.pi
sun_object = bpy.data.objects.new("Sun", sun)
scene.collection.objects.link(sun_object)
camera = scene.camera
camera.location = (0.0, -2.0, 3.0)
camera.rotation_euler = (math.radians(33.7), 0.0, 0.0)
""",
    )
    sun = run(headless, 'result = astra_meter([bpy.data.objects["Card"]], samples=64)')
    assert sun["samples"] >= 32
    assert sun["irradiance"] == pytest.approx(1.0, rel=0.01)
    assert sun["ev"] == pytest.approx(0.0, abs=0.02)
    assert sun["lights"][0]["name"] == "Sun" and sun["key_fill"] is None
    setup(
        headless,
        """
scene = bpy.context.scene
bpy.data.objects.remove(bpy.data.objects["Sun"])
point = bpy.data.lights.new("Bulb", "POINT")
point.energy = 1000.0
bulb = bpy.data.objects.new("Bulb", point)
bulb.location = (0.0, 0.0, 2.0)
scene.collection.objects.link(bulb)
""",
    )
    bulb = run(headless, 'result = astra_meter([bpy.data.objects["Card"]], samples=64)')
    expected = 1000.0 / (4 * math.pi**2 * 4.0)
    assert bulb["irradiance"] == pytest.approx(expected, rel=0.02)
    assert bulb["ambient_share"] == 0.0
    setup(
        headless,
        """
scene = bpy.context.scene
blocker = bpy.data.objects.new("Umbrella", bpy.data.meshes.new("UmbrellaMesh"))
blocker.data.from_pydata([(-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)], [], [(0, 1, 2, 3)])
scene.collection.objects.link(blocker)
""",
    )
    shaded = run(headless, 'result = astra_meter([bpy.data.objects["Card"]], samples=64)')
    assert shaded["irradiance"] == pytest.approx(0.0, abs=1e-6)
    assert shaded["lights"][0]["shadowed"] == pytest.approx(1.0) and shaded["lights"][0]["shadowed_by"] == "Umbrella"


def test_world_bvh_is_in_world_space_and_supports_are_found(headless):
    setup(
        headless,
        """
scene = bpy.context.scene
mesh = bpy.data.meshes["Cube"]
floor_mesh = bpy.data.meshes.new("FloorMesh")
floor_mesh.from_pydata([(-10, -10, 0), (10, -10, 0), (10, 10, 0), (-10, 10, 0)], [], [(0, 1, 2, 3)])
floor = bpy.data.objects.new("Floor", floor_mesh)
scene.collection.objects.link(floor)
bpy.data.objects["Cube"].location = (0.0, 0.0, 1.0)
for name, location in (("Floating", (4.0, 0.0, 1.5)), ("Sunk", (-4.0, 0.0, 0.8)), ("Twin", (0.5, 0.3, 1.4))):
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    scene.collection.objects.link(obj)
""",
    )
    data = run(
        headless,
        """
astra_update()
objects = bpy.data.objects
cube, twin, floating = objects["Cube"], objects["Twin"], objects["Floating"]
overlap = len(astra_world_bvh(cube).overlap(astra_world_bvh(twin)))
apart = len(astra_world_bvh(cube).overlap(astra_world_bvh(floating)))
floor = [objects["Floor"]]
result = {"overlap": overlap, "apart": apart,
          "resting": astra_support_ray(cube, floor), "floating": astra_support_ray(floating, floor),
          "sunk": astra_support_ray(objects["Sunk"], floor),
          "nothing": astra_support_ray(cube, [objects["Floating"]])}
""",
    )
    assert data["overlap"] > 0 and data["apart"] == 0
    assert data["resting"] == {"on": "Floor", "gap_m": pytest.approx(0.0, abs=1e-5)}
    assert data["floating"] == {"on": "Floor", "gap_m": pytest.approx(0.5, abs=1e-5)}
    assert data["sunk"] == {"on": "Floor", "gap_m": pytest.approx(-0.2, abs=1e-5)}
    assert data["nothing"] == {"on": None, "gap_m": None}


def test_helper_scripts_never_leave_stray_data(headless):
    before = json.loads(headless.get_scene_info())
    run(headless, "result = {'ok': True}")
    assert json.loads(headless.get_scene_info()) == before
    assert math.isfinite(1.0)


def test_blind_meter_predicts_a_real_cycles_render(headless):
    # The meter is only worth trusting if it predicts what the renderer
    # produces: a 0.18 grey card under a sun of strength pi should render at
    # linear 0.18. Direct light only (0 bounces), as the meter models it.
    setup(
        headless,
        """
scene = bpy.context.scene
for obj in list(scene.objects):
    if obj.type in {"LIGHT", "MESH"}:
        bpy.data.objects.remove(obj)
scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.0
mesh = bpy.data.meshes.new("Card")
mesh.from_pydata([(-3.0, -3.0, 0.0), (3.0, -3.0, 0.0), (3.0, 3.0, 0.0), (-3.0, 3.0, 0.0)], [], [(0, 1, 2, 3)])
card = bpy.data.objects.new("Card", mesh)
scene.collection.objects.link(card)
sun = bpy.data.lights.new("Sun", "SUN")
sun.energy = math.pi * 0.5
sun_object = bpy.data.objects.new("Sun", sun)
sun_object.rotation_euler = (math.radians(30.0), 0.0, 0.0)
scene.collection.objects.link(sun_object)
camera = scene.camera
camera.location = (0.0, 0.0, 3.0)
camera.rotation_euler = (0.0, 0.0, 0.0)
""",
    )
    data = run(
        headless,
        """
from array import array
scene = bpy.context.scene
card = bpy.data.objects["Card"]
predicted = astra_meter([card], samples=100)
astra_render_snapshot(scene)
grey = bpy.data.materials.new("AstraGrey18")
shader = grey.node_tree.nodes["Principled BSDF"]
shader.inputs["Base Color"].default_value = (0.18, 0.18, 0.18, 1.0)
shader.inputs["Roughness"].default_value = 1.0
shader.inputs["Specular IOR Level"].default_value = 0.0
layer = bpy.context.view_layer
path = bpy.app.tempdir + "astra_meter_check.exr"
try:
    render = scene.render
    render.engine = "CYCLES"
    render.resolution_x, render.resolution_y, render.resolution_percentage = 64, 36, 100
    render.image_settings.media_type = "IMAGE"
    render.image_settings.file_format = "OPEN_EXR"
    render.image_settings.color_depth = "32"
    render.filepath = path
    scene.cycles.samples = 32
    scene.cycles.max_bounces = 0
    scene.cycles.use_denoising = False
    layer.material_override = grey
    bpy.ops.render.render(write_still=True)
finally:
    layer.material_override = None
    failed = astra_render_restore(scene)
    bpy.data.materials.remove(grey)
image = bpy.data.images.load(path, check_existing=False)
pixels = array("f", [0.0]) * (image.size[0] * image.size[1] * image.channels)
image.pixels.foreach_get(pixels)
channels = image.channels
bpy.data.images.remove(image)
total = 0.0
count = len(pixels) // channels
for index in range(count):
    base = index * channels
    total += astra_luma((pixels[base], pixels[base + 1], pixels[base + 2]))
result = {"predicted": predicted["irradiance"] * ASTRA_GREY, "measured": total / count, "ev": predicted["ev"],
          "failed": failed, "engine": scene.render.engine}
""",
    )
    # sun strength pi/2 at 30 degrees from vertical: irradiance 0.5 * cos(30) = 0.433.
    assert data["predicted"] == pytest.approx(0.18 * 0.5 * math.cos(math.radians(30)), rel=0.01)
    assert data["measured"] == pytest.approx(data["predicted"], rel=0.03)
    assert data["failed"] == [] and data["engine"] == "BLENDER_EEVEE"
