"""Blender side of the headless backend. Runs INSIDE Blender, never imported by Astra.

Started as `blender --background --factory-startup --python headless_worker.py`
by astra_blender.headless. It reads one JSON request per stdin line and writes
one "ASTRA_BRIDGE:" + JSON reply per stdout line; Blender's own console
output is ignored by the host because it lacks that prefix.

It mirrors the upstream blender-mcp add-on (blender_mcp/bundled/addon.py):
code runs as exec(code, {"bpy": bpy}) with stdout captured, and the scene and
object summaries have the add-on's exact shape. Safe mode is enforced by the
host before anything is sent here.
"""

import contextlib
import io
import json
import sys
import traceback

import bpy
import mathutils

PREFIX = "ASTRA_BRIDGE:"
OUT = sys.stdout


def reply(payload):
    OUT.write(PREFIX + json.dumps(payload) + "\n")
    OUT.flush()


def execute_code(code):
    """addon.execute_code, plus the partial stdout and line the add-on discards."""
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exec(compile(code, "<astra>", "exec"), {"bpy": bpy})
    except Exception as error:  # noqa: BLE001 - every script error goes back to the host
        frames = traceback.extract_tb(error.__traceback__)
        line = next((frame.lineno for frame in reversed(frames) if frame.filename == "<astra>"), None)
        return {"ok": False, "error": str(error), "line": line, "stdout": buffer.getvalue()}
    return {"ok": True, "stdout": buffer.getvalue()}


def scene_info():
    """addon.get_scene_info: name, counts and the first 10 objects."""
    try:
        info = {
            "name": bpy.context.scene.name,
            "object_count": len(bpy.context.scene.objects),
            "objects": [],
            "materials_count": len(bpy.data.materials),
        }
        for index, obj in enumerate(bpy.context.scene.objects):
            if index >= 10:
                break
            info["objects"].append(
                {
                    "name": obj.name,
                    "type": obj.type,
                    "location": [
                        round(float(obj.location.x), 2),
                        round(float(obj.location.y), 2),
                        round(float(obj.location.z), 2),
                    ],
                }
            )
        return {"ok": True, "result": info}
    except Exception as error:  # noqa: BLE001
        return {"ok": True, "result": {"error": str(error)}}


def aabb(obj):
    corners = [obj.matrix_world @ mathutils.Vector(corner) for corner in obj.bound_box]
    low = mathutils.Vector(map(min, zip(*corners)))
    high = mathutils.Vector(map(max, zip(*corners)))
    return [[*low], [*high]]


def object_info(name):
    """addon.get_object_info; an unknown name is an error, as in the add-on."""
    obj = bpy.data.objects.get(name)
    if not obj:
        return {"ok": False, "error": f"Object not found: {name}"}
    info = {
        "name": obj.name,
        "type": obj.type,
        "location": [obj.location.x, obj.location.y, obj.location.z],
        "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
        "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
        "visible": obj.visible_get(),
        "materials": [],
    }
    if obj.type == "MESH":
        info["world_bounding_box"] = aabb(obj)
    for slot in obj.material_slots:
        if slot.material:
            info["materials"].append(slot.material.name)
    if obj.type == "MESH" and obj.data:
        info["mesh"] = {
            "vertices": len(obj.data.vertices),
            "edges": len(obj.data.edges),
            "polygons": len(obj.data.polygons),
        }
    return {"ok": True, "result": info}


def screenshot():
    """addon.get_viewport_screenshot in background mode: it always fails.

    The add-on first looks for a VIEW_3D area ("No 3D viewport found"); even
    with one, background Blender has no GPU context to draw it.
    """
    try:
        areas = [area for area in bpy.context.screen.areas if area.type == "VIEW_3D"]
    except Exception as error:  # noqa: BLE001
        return {"ok": True, "result": {"error": str(error)}}
    if not areas:
        return {"ok": True, "result": {"error": "No 3D viewport found"}}
    return {"ok": True, "result": {"error": "Blender runs in background mode; there is no viewport to capture"}}


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=False)
    return {"ok": True, "result": {"objects": sorted(obj.name for obj in bpy.context.scene.objects)}}


def main():
    reply({"ready": bpy.app.version_string})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        op = request.get("op")
        if op == "quit":
            break
        if op == "exec":
            response = execute_code(request["code"])
        elif op == "scene_info":
            response = scene_info()
        elif op == "object_info":
            response = object_info(request.get("name", ""))
        elif op == "screenshot":
            response = screenshot()
        elif op == "reset":
            response = reset()
        elif op == "ping":
            response = {"ok": True, "result": {"pong": True}}
        else:
            response = {"ok": False, "error": f"Unknown request {op!r}"}
        response["id"] = request.get("id")
        reply(response)


main()
