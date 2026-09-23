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

import base64
import contextlib
import io
import json
import os
import sys
import traceback
import uuid

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


def camera_render(max_size):
    """A quick Workbench render of the scene camera as base64 PNG.

    Background Blender has no viewport to capture, so this is the stand-in
    headless_connect(screenshots=True) offers vision runs. It restores every
    render setting it touches and deletes its temporary file.
    """
    scene = bpy.context.scene
    if scene.camera is None:
        return {"ok": False, "error": "No scene camera to render (headless Blender has no viewport)"}
    render = scene.render
    image = render.image_settings
    saved = [
        (render, "engine", render.engine),
        (render, "resolution_x", render.resolution_x),
        (render, "resolution_y", render.resolution_y),
        (render, "resolution_percentage", render.resolution_percentage),
        (render, "filepath", render.filepath),
        (render, "film_transparent", render.film_transparent),
        (image, "media_type", image.media_type),
        (image, "file_format", image.file_format),
        (image, "color_mode", image.color_mode),
        (image, "color_depth", image.color_depth),
    ]
    width = render.resolution_x * render.resolution_percentage / 100
    height = render.resolution_y * render.resolution_percentage / 100
    scale = min(1.0, float(max_size) / max(width, height))
    path = os.path.join(bpy.app.tempdir, f"astra_camera_{uuid.uuid4().hex}.png")
    try:
        render.engine = "BLENDER_WORKBENCH"
        render.resolution_x = max(16, round(width * scale))
        render.resolution_y = max(16, round(height * scale))
        render.resolution_percentage = 100
        render.film_transparent = False
        image.media_type = "IMAGE"
        image.file_format = "PNG"
        image.color_mode = "RGB"
        image.color_depth = "8"
        render.filepath = path
        bpy.ops.render.render(write_still=True)
        with open(path, "rb") as stream:
            data = stream.read()
        size = [render.resolution_x, render.resolution_y]
    except Exception as error:  # noqa: BLE001 - reported to the host as a failed screenshot
        return {"ok": False, "error": f"Camera render failed: {error}"}
    finally:
        for owner, name, value in saved:
            try:
                setattr(owner, name, value)
            except (TypeError, ValueError, AttributeError):
                pass
        if os.path.exists(path):
            os.remove(path)
    return {"ok": True, "result": {"png": base64.b64encode(data).decode("ascii"), "size": size}}


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
        elif op == "camera_render":
            response = camera_render(request.get("max_size", 800))
        elif op == "reset":
            response = reset()
        elif op == "ping":
            response = {"ok": True, "result": {"pong": True}}
        else:
            response = {"ok": False, "error": f"Unknown request {op!r}"}
        response["id"] = request.get("id")
        reply(response)


main()
