"""Shared schema pieces for the motion tools, and the motion sweep summary."""

import math
import re

NAME = {"type": "string", "minLength": 1, "maxLength": 128}
NAMES = {"type": "array", "items": NAME, "minItems": 1, "maxItems": 100, "uniqueItems": True}
VECTOR = {
    "type": "array",
    "items": {"type": "number", "minimum": -100000, "maximum": 100000},
    "minItems": 3,
    "maxItems": 3,
}


def tools():
    """The motion tools the model may be offered, as MCP Tools. A 0.3.2 entry
    point, kept as a shim: their specs live in tools/motion.py."""
    from . import registry

    return [
        spec.as_tool()
        for spec in registry.all_specs()
        if spec.module == "astra_blender.tools.motion" and not spec.hidden
    ]


def _overlaps(a, b):
    return all(a[0][i] <= b[1][i] and b[0][i] <= a[1][i] for i in range(3))


def _gap(a, b):
    return math.sqrt(sum(max(0, a[0][i] - b[1][i], b[0][i] - a[1][i]) ** 2 for i in range(3)))


def sweep_summary(sweep, limit=24):
    """Turn per-frame boxes into the few facts worth a model's attention.

    New overlaps: pairs that do not intersect at the first sampled frame but do
    later - a hint of collision, not a mesh-level verdict. Ground breaches: an
    object dipping below a broad flat object named like a floor. Detached
    parts: assembled pieces whose gap to their anchor exceeds the allowed
    maximum at any sampled frame.
    """
    frames = sweep.get("frames", [])
    bounds = sweep.get("bounds", {})
    keys = sorted(bounds)
    summary = {
        "frames": frames,
        "new_overlaps": [],
        "below_ground": [],
        "detached": [],
        "limits": "Boxes at sampled frames only. Overlapping boxes are not proof of intersecting meshes, and "
        "nothing between two samples is observed.",
    }
    if not frames or not keys:
        return summary
    floors = [
        key
        for key in keys
        if re.search(r"ground|floor|suelo|terrain", key, re.I)
        and (bounds[key][0][1][2] - bounds[key][0][0][2])
        < 0.1 * min(bounds[key][0][1][0] - bounds[key][0][0][0], bounds[key][0][1][1] - bounds[key][0][0][1])
    ]
    solids = [key for key in keys if key not in floors]
    for index, a in enumerate(solids):
        for b in solids[index + 1 :]:
            if _overlaps(bounds[a][0], bounds[b][0]):
                continue  # Touching or nested from the start reads as intentional.
            for position, frame in enumerate(frames[1:], 1):
                if _overlaps(bounds[a][position], bounds[b][position]):
                    summary["new_overlaps"].append({"objects": [a, b], "first_frame": frame})
                    break
            if len(summary["new_overlaps"]) >= limit:
                break
        if len(summary["new_overlaps"]) >= limit:
            break
    for key in solids:
        for floor in floors:
            top = bounds[floor][0][1][2]
            hits = [
                frames[position]
                for position in range(len(frames))
                if bounds[key][position][0][2] < top - 0.02
                and all(
                    bounds[key][position][1][i] > bounds[floor][position][0][i]
                    and bounds[key][position][0][i] < bounds[floor][position][1][i]
                    for i in (0, 1)
                )
            ]
            if hits:
                summary["below_ground"].append({"object": key, "ground": floor, "frames": hits[:16]})
                break
    for key, anchor in sweep.get("anchors", {}).items():
        if key not in bounds or anchor not in bounds or key == anchor:
            continue
        allowed = float(sweep.get("max_gaps", {}).get(key, 0.15))
        worst, at = 0.0, None
        for position, frame in enumerate(frames):
            gap = _gap(bounds[key][position], bounds[anchor][position])
            if gap > worst:
                worst, at = gap, frame
        if worst > allowed:
            summary["detached"].append({"objects": [key, anchor], "max_gap": round(worst, 4), "frame": at})
    return summary


def report(data, diagnose):
    result = {
        **data,
        "samples": [{"frame": s["frame"], "scene": diagnose(s["scene"])} for s in data["samples"]],
    }
    if "sweep" in data:
        result["sweep"] = sweep_summary(data["sweep"])
    return result
