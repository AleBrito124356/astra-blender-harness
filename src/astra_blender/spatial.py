"""Model-facing numerical evidence and trusted Blender inspection tools."""

import json
import math
import re
from pathlib import Path

from mcp.types import Tool

from . import motion

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
            + ", "
            + repr(arguments.get("frames"))
            + "))"
        )
    )


def bake_code(arguments):
    """Bake world matrices of every moving object across the scene frame range."""
    return (
        (SCRIPTS / "motion_bake.py").read_text(encoding="utf-8")
        + "\nimport json\nprint("
        + repr(MARKER)
        + " + json.dumps(astra_motion_bake(step="
        + repr(int(arguments.get("step", 1)))
        + "), allow_nan=False))"
    )


MOTION_FUNCTIONS = {
    "astra_assemble_parts": ("assembly.py", "astra_assemble"),
    "astra_place_on_ground": ("assembly.py", "astra_ground"),
    "astra_keyframe_object": ("animation.py", "astra_keyframes"),
    "astra_inspect_animation": ("animation.py", "astra_animation_report"),
}


def tool_code(name, args):
    if name == "astra_inspect_scene":
        return probe_code()
    if name == "astra_frame_camera":
        return frame_code(args)
    script, function = MOTION_FUNCTIONS[name]
    prefix = ""
    if name in {"astra_inspect_animation", "astra_keyframe_object"}:
        prefix = (
            (SCRIPTS / "projection.py").read_text(encoding="utf-8")
            + "\n"
            + (SCRIPTS / "scene_probe.py").read_text(encoding="utf-8")
            + "\n"
        )
    return (
        prefix
        + (SCRIPTS / script).read_text(encoding="utf-8")
        + "\nimport json\nprint("
        + repr(MARKER)
        + " + json.dumps("
        + function
        + "(**"
        + repr(args)
        + "), allow_nan=False))"
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
    # Ground checks require an identifiable broad horizontal support, not an assumed z=0.
    floors = [
        o
        for o in objects
        if re.search(r"ground|floor|suelo|terrain", o["name"], re.I)
        and o["dimensions"][2] < min(o["dimensions"][:2]) * 0.1
    ]
    by_name = {o["name"]: o for o in objects}
    anchors = [
        o for o in objects if re.search(r"^(car|vehicle|coche|auto).*(body|lower|chassis)", o["name"], re.I)
    ]
    for obj in objects:
        for floor in floors:
            if obj is floor or not all(
                obj["bounds"][1][i] > floor["bounds"][0][i] and obj["bounds"][0][i] < floor["bounds"][1][i]
                for i in (0, 1)
            ):
                continue
            top = floor["bounds"][1][2]
            if obj["bounds"][0][2] < top - max(0.02, obj["dimensions"][2] * 0.02):
                issues.append(
                    {
                        "level": "warning",
                        "code": "below_ground",
                        "objects": [obj["name"]],
                        "ground": floor["name"],
                        "depth": round(top - obj["bounds"][0][2], 4),
                        "message": "Object extends below the ground support. Check intentional burial versus misplaced parts.",
                    }
                )
                break
        anchor = by_name.get(obj.get("anchor"))
        inferred = False
        if not anchor and re.search(r"car|vehicle|coche", obj["name"], re.I):
            anchor = next((a for a in anchors if a is not obj), None)
            inferred = True
        if anchor and anchor is not obj:
            gap = math.sqrt(
                sum(
                    max(
                        0,
                        anchor["bounds"][0][i] - obj["bounds"][1][i],
                        obj["bounds"][0][i] - anchor["bounds"][1][i],
                    )
                    ** 2
                    for i in range(3)
                )
            )
            tolerance = (
                max(0.05, max(anchor["dimensions"]) * 0.06) if inferred else float(obj.get("max_gap", 0.15))
            )
            if gap > tolerance:
                issues.append(
                    {
                        "level": "warning",
                        "code": "detached_part",
                        "objects": [obj["name"], anchor["name"]],
                        "gap": round(gap, 4),
                        "inferred_anchor": inferred,
                        "message": "Part bounds are separated from the expected main body. Check placement before parenting or animating.",
                    }
                )
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
        "timeline": snapshot.get("timeline"),
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
            "its viewing direction. For animation pass up to five frames to fit combined motion bounds; restores playhead. Inspect again before rendering.",
            inputSchema={
                "type": "object",
                "properties": {
                    "objects": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
                    "margin": {"type": "number", "minimum": 0.02, "maximum": 0.35},
                    "frames": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {"type": "integer", "minimum": 1, "maximum": 100000},
                    },
                },
                "required": ["objects"],
                "additionalProperties": False,
            },
        ),
    ] + motion.tools()
