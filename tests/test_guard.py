"""Astra Guard accepts ordinary scene code and rejects the constructs hostile code needs."""

import pytest

from astra_blender import guard, spatial

ACCEPTED = {
    "primitives": """
import bpy
from mathutils import Vector, Matrix
import math, random
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
cube = bpy.context.active_object
cube.name = "Body"
cube.rotation_euler.z = math.radians(15)
mod = cube.modifiers.new("Bevel", "BEVEL")
mod.width = 0.02
print(cube.dimensions)
""",
    "material": """
import bpy
mat = bpy.data.materials.get("Clay") or bpy.data.materials.new("Clay")
mat.use_nodes = True
bsdf = mat.node_tree.nodes.get("Principled BSDF")
for name, value in {"Roughness": 0.45, "Metallic": 0.0}.items():
    bsdf.inputs[name].default_value = value
for attr in ("location", "scale"):
    print(attr, getattr(bpy.data.objects["Body"], attr))
setattr(bpy.data.objects["Body"], "hide_render", False)
""",
    "bmesh_arrays": """
import bpy, bmesh
from array import array
mesh = bpy.data.meshes.new("Grid")
bm = bmesh.new()
bmesh.ops.create_grid(bm, x_segments=4, y_segments=4, size=1)
bm.to_mesh(mesh)
bm.free()
coords = array("f", [0.0]) * (len(mesh.vertices) * 3)
mesh.vertices.foreach_get("co", coords)
print(type(coords).__name__, "{:.2f} {:,.1f}".format(sum(coords), 1234.5))
""",
    "operator_aliases": """
import bpy
ops = bpy.ops
mesh_ops = bpy.ops.mesh
mesh_ops.primitive_cylinder_add(vertices=48, radius=0.4, depth=1.2)
ops.object.shade_smooth()
if hasattr(bpy.ops.mesh, "primitive_torus_add"):
    getattr(bpy.ops.mesh, "primitive_torus_add")(location=(0, 0, 2))
bpy.ops.object.mode_set(mode="OBJECT")
C = bpy.context
C.view_layer.update()
print(isinstance(C.object, bpy.types.Object))
""",
    "render_and_settings": """
import bpy
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.samples = 32
scene.view_settings.view_transform = "AgX"
prefs = bpy.context.preferences.addons["cycles"].preferences
prefs.compute_device_type = "NONE"
scene.render.filepath = "/tmp/out/final.png"
bpy.ops.render.render(write_still=True)
print("{} objects".format(len(scene.objects)), f"{scene.frame_end}")
""",
    "animation_and_driver": """
import bpy
obj = bpy.data.objects["Body"]
obj.keyframe_insert(data_path="location", frame=1)
fcurve = obj.driver_add("rotation_euler", 2)
fcurve.driver.expression = "frame * 0.1"
if __name__ == "__main__":
    print("done")
""",
    "imports_and_dir": """
import bpy
from bpy_extras.object_utils import world_to_camera_view
from collections import defaultdict
import json, time
print(json.dumps(sorted(n for n in dir(bpy.types.Object) if n.startswith("loc"))))
print(time.perf_counter() > 0)
bpy.ops.import_scene.gltf(filepath="/tmp/asset.glb")
""",
}

REJECTED = {
    "os_import": ("import os\nos.system('rm -rf ~')", "import of 'os'"),
    "subprocess_from": ("from subprocess import run\nrun(['ls'])", "import of 'subprocess'"),
    "importlib": ("import importlib\nimportlib.import_module('os')", "import of 'importlib'"),
    "dunder_import": ("__import__('os').system('id')", "__import__"),
    "eval": ("eval('1+1')", "eval"),
    "exec": ("exec('print(1)')", "exec"),
    "open": ("open('/etc/passwd').read()", "open"),
    "class_ladder": ("().__class__.__base__.__subclasses__()", "__class__"),
    "computed_getattr": ("import bpy\nname = '__cl' + 'ass__'\ngetattr(bpy, name)", "literal attribute name"),
    "getattr_dunder_literal": ("getattr(print, '__globals__')", "__globals__"),
    "getattr_as_value": ("import functools\nfunctools.reduce(getattr, ['a'], object)", "called directly"),
    "format_escape": ("'{0.__class__}'.format(1)", "format fields"),
    "nested_format_escape": ("'{0:{1.__class__}}'.format(1, 2)", "format fields"),
    "computed_format": ("s = '{0.' + 'x}'\ns.format(1)", "string literal"),
    "handlers": (
        "import bpy\nbpy.app.handlers.frame_change_post.append(lambda s: None)",
        ".handlers",
    ),
    "timers_alias": ("from bpy.app import timers\ntimers.register(print)", "timers"),
    "texts": ("import bpy\nbpy.data.texts.new('x').write('import os')", ".texts"),
    "libraries": (
        "import bpy\nwith bpy.data.libraries.load('/tmp/evil.blend') as (a, b): pass",
        ".libraries",
    ),
    "register_class": ("import bpy\nbpy.utils.register_class(object)", "register_class"),
    "script_op": ("import bpy\nbpy.ops.script.reload()", "bpy.ops.script"),
    "aliased_ops": (
        "import bpy\nops = bpy.ops\nops.preferences.addon_enable(module='x')",
        "bpy.ops.preferences",
    ),
    "from_ops": ("from bpy.ops import wm\nwm.append(filename='x')", "bpy.ops.wm.append"),
    "save_over_original": ("import bpy\nbpy.ops.wm.save_mainfile()", "save_mainfile"),
    "open_file": ("import bpy\nbpy.ops.wm.open_mainfile(filepath='/tmp/x.blend')", "open_mainfile"),
    "autoexec": (
        "import bpy\nbpy.context.preferences.filepaths.use_scripts_auto_execute = True",
        "use_scripts_auto_execute",
    ),
    "frame_walk": ("try:\n    1/0\nexcept Exception as e:\n    e.__traceback__", "__traceback__"),
    "tb_frame": ("import sys", "import of 'sys'"),
    "numpy": ("import numpy as np\nnp.load('/tmp/x.npy', allow_pickle=True)", "import of 'numpy'"),
    "tofile": ("x = bpy_array()\nx.tofile('/tmp/x')", "tofile"),
    "typing_eval": ("import typing\ntyping.get_type_hints(f)", "import of 'typing'"),
    "singledispatch": ("import functools\n@functools.singledispatch\ndef f(x): pass", "singledispatch"),
    "string_formatter": (
        "import string\nstring.Formatter().vformat('{0.x}', (1,), {})",
        "import of 'string'",
    ),
    # Receivers the guard cannot resolve must not hide a refused operator.
    "walrus_wm": ("import bpy\n(w := bpy.ops.wm).save_mainfile()", "save_mainfile"),
    "walrus_ops_append": ("import bpy\n(w := bpy.ops.wm).append(filepath='/x.blend')", "bpy.ops.wm"),
    "subscript_wm": ("import bpy\n[bpy.ops.wm][0].append(filepath='/x.blend')", "bpy.ops.wm"),
    "lambda_ops": (
        "import bpy\n(lambda: bpy.ops)().script.python_file_run(filepath='/x.py')",
        "python_file_run",
    ),
    "lambda_group": ("import bpy\n(lambda: bpy.ops)().wm.link(filepath='/x.blend')", "bpy.ops"),
    "walrus_prefs": ("import bpy\n(o := bpy.ops).preferences.addon_enable(module='x')", "addon_enable"),
    "loop_getattr_ops": (
        "import bpy\nfor g in ('preferences',):\n    getattr(bpy.ops, g).addon_install(filepath='x')",
        "addon_install",
    ),
    "loop_getattr_wm": (
        "import bpy\nfor o in ('append',):\n    getattr(bpy.ops.wm, o)(filepath='/x')",
        "bpy.ops.wm.append",
    ),
    "loop_getattr_namespace": (
        "import bpy\nfor g in ('wm',):\n    getattr(bpy.ops, g)",
        "bpy.ops may only be used directly",
    ),
    "argument": (
        "import bpy\ndef f(m):\n    m.append(filepath='/x')\nf(bpy.ops.wm)",
        "bpy.ops.wm may only be used directly",
    ),
    "container": (
        "import bpy\nd = {'a': bpy.ops.wm}\nd['a'].append(filepath='/x')",
        "bpy.ops.wm may only be used directly",
    ),
    "default_arg": (
        "import bpy\ndef f(m=bpy.ops.wm):\n    m.link(filepath='/x')",
        "bpy.ops.wm may only be used directly",
    ),
    "return_value": (
        "import bpy\ndef f():\n    return bpy.ops.wm\nf().append(filepath='/x')",
        "bpy.ops.wm may only be used directly",
    ),
    "branch_alias": (
        "import bpy\nif x:\n    w = bpy.ops.wm\nelse:\n    w = bpy.ops.mesh\nw.append(filepath='/x')",
        "bpy.ops.wm.append",
    ),
    "alias_chain": ("import bpy\na = bpy\nb = a.ops\nc = b.wm\nc.link(filepath='/x')", "bpy.ops.wm.link"),
    "conditional": (
        "import bpy\n(bpy.ops.wm if x else bpy.ops.mesh).append(filepath='/x')",
        "may only be used directly",
    ),
    "bpy_as_value": ("import bpy\n[bpy][0].ops.wm.append(filepath='/x')", "bpy may only be used directly"),
    "unresolved_open": ("thing = make()\nthing.open_mainfile(filepath='/x')", "open_mainfile"),
    "import_handlers": ("import bpy.app.handlers", "import of 'bpy.app.handlers'"),
    "driver_injection": (
        "import bpy\nd = bpy.data.objects['A'].driver_add('location', 0)\n"
        "d.driver.expression = \"__import__('os').getcwd()\"",
        "driver expressions",
    ),
    "computed_driver": ("x = 'a'\nobj.driver.expression = x", "string literal"),
    "builtins_name": ("__builtins__['eval']('1')", "__builtins__"),
    "star_import": ("from mathutils import *", "import *"),
    "syntax": ("def broken(:\n    pass", "SyntaxError"),
}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_ordinary_scene_code_is_accepted(name):
    assert guard.check(ACCEPTED[name]) == []


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_hostile_constructs_are_rejected(name):
    code, expected = REJECTED[name]
    violations = guard.check(code)
    assert violations, f"{name} was accepted"
    assert any(expected in v.message for v in violations), [str(v) for v in violations]


def test_one_message_per_forbidden_chain():
    violations = guard.check("import bpy\nbpy.ops.script.reload()")
    assert len(violations) == 1
    assert violations[0].line == 2


def test_rejection_text_names_the_line_and_the_way_out():
    with pytest.raises(guard.GuardRejected) as caught:
        guard.validate("import os\nimport bpy\nbpy.ops.wm.save_mainfile()")
    text = guard.rejection_text(caught.value)
    assert text.startswith("TOOL ERROR: Astra Guard")
    assert "line 1" in text and "line 3" in text
    assert "Astra saves scene files itself" in text


@pytest.mark.parametrize(
    "name",
    ["astra_inspect_scene", "astra_frame_camera", "astra_inspect_animation", "astra_keyframe_object"],
)
def test_astra_trusted_scripts_would_pass_the_guard(name):
    # They never go through it (no model text in them), but the reviewed
    # scripts staying within the same rules keeps them safe-mode compatible too.
    arguments = {
        "astra_frame_camera": {"objects": ["A"]},
        "astra_keyframe_object": {"object": "A", "keys": [{"frame": 1, "location": [0, 0, 0]}]},
    }.get(name, {})
    assert guard.check(spatial.tool_code(name, arguments)) == []
