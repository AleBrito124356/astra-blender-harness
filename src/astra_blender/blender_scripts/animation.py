"""Bounded transform animation and evaluated pose inspection."""

import bpy


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
        "restored_frame": original,
        "limits": "Sampled poses cannot prove collision-free motion between samples. Simulations are not baked.",
    }
