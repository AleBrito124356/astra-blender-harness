"""Model-facing numerical evidence and trusted Blender inspection tools."""

import json
from pathlib import Path

from mcp.types import Tool

SCRIPTS = Path(__file__).parent / "blender_scripts"
MARKER = "ASTRA_SCENE_JSON:"


def probe_code(geometry=False):
    return (
        (SCRIPTS / "projection.py").read_text(encoding="utf-8")
        + "\n"
        + (SCRIPTS / "scene_probe.py").read_text(encoding="utf-8")
        + (
            "\nimport json\nprint("
            + repr(MARKER)
            + " + json.dumps(astra_scene_probe("
            + repr(geometry)
            + "), allow_nan=False))"
        )
    )


def frame_code(arguments):
    return (
        (SCRIPTS / "projection.py").read_text(encoding="utf-8")
        + "\n"
        + (SCRIPTS / "frame_camera.py").read_text(encoding="utf-8")
        + (
            "\nprint(astra_frame_camera("
            + repr(arguments["objects"])
            + ", "
            + repr(arguments.get("margin", 0.12))
            + "))"
        )
    )


def parse_probe(result):
    for block in result.content:
        if block.type != "text":
            continue
        for line in block.text.splitlines():
            if MARKER in line:
                data, _ = json.JSONDecoder().raw_decode(line.split(MARKER, 1)[1])
                if data.get("schema_version") != 1:
                    raise ValueError("Unsupported scene probe version")
                return data
    raise ValueError("Blender did not return scene data; inspect the MCP error or safe-mode settings.")


def diagnostics(snapshot):
    issues = []
    camera = snapshot.get("camera")
    objects = snapshot.get("objects", [])
    if not camera:
        issues.append({"level": "error", "code": "no_camera", "message": "No active render camera."})
    elif objects and camera.get("type", "PERSP") in {"PERSP", "ORTHO"}:
        # Exclude wide flat floors from subject-framing checks.
        subjects = [
            o
            for o in objects
            if min(o["dimensions"]) > 0.01 and max(o["dimensions"]) / max(min(o["dimensions"]), 0.001) < 40
        ]
        outside = [o["name"] for o in subjects if not o.get("camera", {}).get("center_in_frame", False)]
        cropped = [o["name"] for o in subjects if o.get("camera", {}).get("corners_in_frame", 0) < 8]
        if outside:
            issues.append(
                {
                    "level": "warning",
                    "code": "outside_frame",
                    "objects": outside,
                    "message": "Subject centers outside the render frame. Check the camera before rendering.",
                }
            )
        if cropped:
            issues.append(
                {
                    "level": "warning",
                    "code": "cropped_bounds",
                    "objects": cropped,
                    "message": "Subject bounds cross the frame. Use astra_frame_camera with intended subject names.",
                }
            )
    # Bounding-box containment is a hint, not a mesh collision verdict.
    containers = []
    for obj in objects:
        volume = obj["dimensions"][0] * obj["dimensions"][1] * obj["dimensions"][2]
        if volume <= 0.0001:
            continue
        for outer in objects:
            outer_volume = outer["dimensions"][0] * outer["dimensions"][1] * outer["dimensions"][2]
            if outer_volume < volume * 3 or min(outer["dimensions"]) < 0.1:
                continue
            if all(
                outer["bounds"][0][i] <= obj["bounds"][0][i] and obj["bounds"][1][i] <= outer["bounds"][1][i]
                for i in range(3)
            ):
                containers.append({"object": obj["name"], "inside": outer["name"]})
                break
    if containers:
        issues.append(
            {
                "level": "info",
                "code": "contained_bounds",
                "message": "Some object bounds are entirely inside others. Check unintended placement; nested parts may be intentional.",
                "pairs": containers[:24],
            }
        )
    for obj in objects:
        if not obj.get("materials"):
            issues.append(
                {
                    "level": "warning",
                    "code": "no_material",
                    "objects": [obj["name"]],
                    "message": "Geometry has no assigned material.",
                }
            )
    return {
        "schema_version": 1,
        "blender_version": snapshot.get("blender_version"),
        "scene": snapshot.get("scene"),
        "camera": camera,
        "render": snapshot.get("render"),
        "objects": objects,
        "capabilities": snapshot.get("capabilities", {}),
        "issues": issues,
        "warnings": snapshot.get("warnings", []),
        "limits": "World bounding boxes and camera projection are numerical evidence, not proof of appearance or occlusion.",
    }


def tools():
    return [
        Tool(
            name="astra_inspect_scene",
            description="Read actual evaluated world bounds, dimensions, camera "
            "projection, material assignments and current Blender API capabilities. Essential for text-only models. "
            "No images and no scene changes.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="astra_frame_camera",
            description="Fit the active Blender camera around exact subject object "
            "names from astra_inspect_scene. Exclude huge floors/backgrounds. Modifies only the camera; preserves "
            "its viewing direction. Inspect again before rendering.",
            inputSchema={
                "type": "object",
                "properties": {
                    "objects": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
                    "margin": {"type": "number", "minimum": 0.02, "maximum": 0.35},
                },
                "required": ["objects"],
                "additionalProperties": False,
            },
        ),
    ]
