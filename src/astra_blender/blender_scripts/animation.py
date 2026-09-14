"""Bounded transform animation and evaluated pose inspection."""

import bpy
from mathutils import Vector

GEOMETRY = {"MESH", "CURVE", "SURFACE", "FONT", "META"}


def astra_curves(obj):
    animation = obj.animation_data
    if not animation or not animation.action:
        return []
    action = animation.action
    if hasattr(action, "layers") and action.layers:
        curves = []
        slot = animation.action_slot
        if slot:
            for layer in action.layers:
                for strip in layer.strips:
                    if strip.type == "KEYFRAME":
                        bag = strip.channelbag(slot)
                        if bag:
                            curves.extend(list(bag.fcurves))
        return curves
    return list(action.fcurves) if hasattr(action, "fcurves") else []


def astra_keyframes(name, keys, fps=24, interpolation="BEZIER"):
    scene = bpy.context.scene
    obj = scene.objects.get(name)
    if not obj or obj.constraints:
        raise ValueError("Choose an existing unconstrained object or assembly root.")
    if obj.animation_data:
        raise ValueError(
            "This object already has animation data. Inspect and edit its existing action explicitly."
        )
    if obj.rotation_mode != "XYZ" and any("rotation" in key for key in keys):
        raise ValueError("Euler keyframes require XYZ rotation mode. Adapt the rig explicitly.")
    frames = [k["frame"] for k in keys]
    if frames != sorted(set(frames)):
        raise ValueError("Keyframes must use distinct, increasing frame numbers.")
    for channel in ("location", "rotation", "scale"):
        count = sum(channel in k for k in keys)
        if count == 1:
            raise ValueError("Each animated channel needs at least two keys.")
    other_actions = any(o != obj and o.animation_data and o.animation_data.action for o in scene.objects)
    original = scene.frame_current
    try:
        for key in keys:
            frame = key["frame"]
            for field, path in [("location", "location"), ("rotation", "rotation_euler"), ("scale", "scale")]:
                if field in key:
                    if field == "location":
                        obj.location = key[field]
                    elif field == "rotation":
                        obj.rotation_euler = key[field]
                    else:
                        obj.scale = key[field]
                    obj.keyframe_insert(data_path=path, frame=frame, group="Astra Motion")
        for curve in astra_curves(obj):
            for point in curve.keyframe_points:
                point.interpolation = interpolation
        scene.frame_start = min(scene.frame_start, frames[0]) if other_actions else frames[0]
        scene.frame_end = max(scene.frame_end, frames[-1]) if other_actions else frames[-1]
        scene.render.fps = fps
        scene.render.fps_base = 1
    finally:
        scene.frame_set(original)
    return {
        "object": name,
        "frames": [k["frame"] for k in keys],
        "fps": fps,
        "message": "Local transform keys created. Attached children follow their parent; rotations use radians.",
    }


def astra_sweep_frames(start, end, count=16):
    if end <= start:
        return [start]
    return sorted({start + round((end - start) * i / (count - 1)) for i in range(count)})


def astra_motion_sweep(frames):
    """World bounds of every visible geometry instance at each frame.

    Three sampled poses cannot say what happens between them. Boxes at sixteen
    frames cost no mesh serialization and are enough to flag parts that start
    intersecting, sink below the ground or drift from their anchor mid-motion.
    The summary is computed by the harness; the model never sees raw boxes.
    """
    scene = bpy.context.scene
    original = scene.frame_current
    bounds, anchors, max_gaps = {}, {}, {}
    try:
        for frame in frames:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            seen = {}
            for instance in depsgraph.object_instances:
                obj = instance.object
                if obj.hide_render or obj.type not in GEOMETRY:
                    continue
                seen[obj.name] = seen.get(obj.name, 0) + 1
                key = obj.name if seen[obj.name] == 1 else f"{obj.name}#{seen[obj.name]}"
                corners = [instance.matrix_world @ Vector(corner) for corner in obj.bound_box]
                low = [round(min(p[i] for p in corners), 4) for i in range(3)]
                high = [round(max(p[i] for p in corners), 4) for i in range(3)]
                bounds.setdefault(key, []).append([low, high])
                source = obj.original
                if source.get("astra_anchor"):
                    anchors[key] = source["astra_anchor"]
                    max_gaps[key] = float(source.get("astra_max_gap", 0.15))
    finally:
        scene.frame_set(original)
    bounds = {key: boxes for key, boxes in bounds.items() if len(boxes) == len(frames)}
    return {"frames": frames, "bounds": bounds, "anchors": anchors, "max_gaps": max_gaps}


def astra_animation_report(frames=None):
    scene = bpy.context.scene
    original = scene.frame_current
    if frames is None:
        frames = sorted(set([scene.frame_start, (scene.frame_start + scene.frame_end) // 2, scene.frame_end]))
    samples = []
    try:
        for frame in frames:
            scene.frame_set(frame)
            samples.append({"frame": frame, "scene": astra_scene_probe(False)})  # noqa: F821
    finally:
        scene.frame_set(original)
    actions = []
    for obj in scene.objects:
        curves = astra_curves(obj)
        if curves:
            actions.append(
                {
                    "object": obj.name,
                    "parent": obj.parent.name if obj.parent else None,
                    "channels": sorted(set(c.data_path for c in curves)),
                    "changing_channels": sorted(
                        set(
                            c.data_path
                            for c in curves
                            if c.keyframe_points
                            and max(p.co.y for p in c.keyframe_points)
                            - min(p.co.y for p in c.keyframe_points)
                            > 0.000001
                        )
                    ),
                    "keyed_frames": sorted(set(round(p.co.x, 3) for c in curves for p in c.keyframe_points))[
                        :100
                    ],
                }
            )
    return {
        "schema_version": 1,
        "start": scene.frame_start,
        "end": scene.frame_end,
        "fps": scene.render.fps / scene.render.fps_base,
        "actions": actions,
        "samples": samples,
        "sweep": astra_motion_sweep(astra_sweep_frames(scene.frame_start, scene.frame_end)),
        "restored_frame": original,
        "limits": "Sampled poses cannot prove collision-free motion between samples. Simulations are not baked.",
    }
