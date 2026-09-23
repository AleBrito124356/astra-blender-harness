"""Model-facing numerical evidence and trusted Blender inspection tools."""

import json
import math
import re
from pathlib import Path

SCRIPTS = Path(__file__).parent / "blender_scripts"
MARKER = "ASTRA_SCENE_JSON:"


def probe_code(geometry=False, known=None):
    """The read-only scene probe. `known` lists geometry hashes already held, so
    Blender serializes only meshes that changed."""
    return (
        (SCRIPTS / "projection.py").read_text(encoding="utf-8")
        + "\n"
        + (SCRIPTS / "scene_probe.py").read_text(encoding="utf-8")
        + (
            "\nimport json\nprint("
            + repr(MARKER)
            + " + json.dumps(astra_scene_probe("
            + repr(bool(geometry))
            + ", known="
            + repr(dict(known or {}))
            + "), allow_nan=False))"
        )
    )


def known_hashes(snapshot):
    """Instance key -> geometry hash for every mesh a snapshot already carries."""
    if not snapshot:
        return {}
    return {
        entry.get("key") or entry["name"]: entry["hash"]
        for entry in snapshot.get("meshes", [])
        if entry.get("hash")
    }


GEOMETRY_FIELDS = ("positions", "triangles", "normals", "material_indices", "proxy")


def merge_unchanged(snapshot, previous):
    """Fill meshes the probe reported as unchanged from the previous snapshot.

    The result is always a complete snapshot, so diagnostics, the viewer and
    the tests never see a partial one. A mesh the server no longer holds - it
    should not happen, but a restart mid-refresh could - falls back to a
    bounding-box proxy rather than a broken entry.
    """
    held = {entry.get("key") or entry["name"]: entry for entry in (previous or {}).get("meshes", [])}
    for entry in snapshot.get("meshes", []):
        if not entry.pop("unchanged", False):
            continue
        source = held.get(entry.get("key") or entry["name"])
        if source is None:
            entry["proxy"] = True
            continue
        for field in GEOMETRY_FIELDS:
            if field in source:
                entry[field] = source[field]
    return snapshot


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


def tool_code(name, args):
    """The script for one trusted tool call. A 0.3.2 entry point, kept as a shim:
    the registry now builds every trusted script (scripts.build)."""
    from . import registry, scripts

    return scripts.build(registry.get(name), args)


def __getattr__(name):
    # MOTION_FUNCTIONS was the 0.3.2 dispatch table, name -> (script, function).
    # It is derived from the registry on access, so it can never drift from it.
    if name == "MOTION_FUNCTIONS":
        from . import registry

        return {
            spec.name: (spec.scripts[-1], spec.entry)
            for spec in registry.all_specs()
            if spec.module == "astra_blender.tools.motion" and spec.kind == "blender"
        }
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    """The trusted tools the model may be offered, as MCP Tools. A 0.3.2 entry
    point, kept as a shim over the registry."""
    from . import registry

    return [spec.as_tool() for spec in registry.all_specs() if not spec.hidden]
