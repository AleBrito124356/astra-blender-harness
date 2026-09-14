"""Trusted motion bake: world matrices per moving object and frame, in one read.

Rigid motion - parts of an assembly separating, wheels turning, a root
translating - changes an object's matrix, not its mesh. So a whole animation
fits in a single Blender call, and the browser can play it at full rate,
scrub without latency and loop, all without touching the playhead or the MCP
connection again. Objects whose mesh itself deforms are reported so the
viewer can say their shape is not previewed.
"""

import bpy

GEOMETRY = {"MESH", "CURVE", "SURFACE", "FONT", "META"}
# Modifier types whose result depends on time or on other objects' motion.
DEFORMING = {
    "ARMATURE",
    "CAST",
    "CLOTH",
    "CURVE",
    "DISPLACE",
    "DYNAMIC_PAINT",
    "EXPLODE",
    "FLUID",
    "HOOK",
    "LAPLACIANDEFORM",
    "LATTICE",
    "MESH_DEFORM",
    "NODES",
    "OCEAN",
    "PARTICLE_SYSTEM",
    "SHRINKWRAP",
    "SIMPLE_DEFORM",
    "SOFT_BODY",
    "SURFACE_DEFORM",
    "WARP",
    "WAVE",
}


def astra_instance_keys(depsgraph):
    """Stable keys shared with the scene probe: the object name, then name#2, name#3
    for further instances of the same object within one frame."""
    seen, keys = {}, []
    for instance in depsgraph.object_instances:
        obj = instance.object
        if obj.hide_render or obj.type not in GEOMETRY:
            keys.append(None)
            continue
        count = seen.get(obj.name, 0) + 1
        seen[obj.name] = count
        keys.append(obj.name if count == 1 else f"{obj.name}#{count}")
    return keys


def astra_moving_objects(scene):
    moving = set()
    for obj in scene.objects:
        data = obj.animation_data
        animated = bool(data and (data.action or data.nla_tracks or data.drivers))
        if animated or obj.constraints or obj.parent_type in {"ARMATURE", "BONE"}:
            moving.add(obj.name)
            moving.update(child.name for child in obj.children_recursive)
    return moving


def astra_deforming_objects(scene, names):
    found = []
    for name in sorted(names):
        obj = scene.objects.get(name)
        if not obj or obj.type not in GEOMETRY:
            continue
        keys = getattr(obj.data, "shape_keys", None)
        if (keys and len(keys.key_blocks) > 1) or any(m.type in DEFORMING for m in obj.modifiers):
            found.append(name)
    return found


def astra_motion_bake(step=1, max_samples=60000):
    scene = bpy.context.scene
    start, end = scene.frame_start, scene.frame_end
    if end < start:
        raise ValueError("Scene frame range is empty.")
    moving = astra_moving_objects(scene)
    original = scene.frame_current
    # Keep the payload bounded: coarser sampling rather than a refused bake.
    total = end - start + 1
    step = max(1, int(step))
    while moving and (total // step + 1) * len(moving) > max_samples:
        step += 1
    frames = list(range(start, end + 1, step))
    if frames[-1] != end:
        frames.append(end)
    tracks = {}
    try:
        for frame in frames:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            keys = astra_instance_keys(depsgraph)
            for key, instance in zip(keys, depsgraph.object_instances):
                if key is None or instance.object.original.name not in moving:
                    continue
                matrix = instance.matrix_world
                tracks.setdefault(key, []).append(
                    [round(matrix[r][c], 5) for c in range(4) for r in range(4)]
                )
    finally:
        scene.frame_set(original)
    # An instance that exists only at some frames cannot be played as a track.
    tracks = {key: samples for key, samples in tracks.items() if len(samples) == len(frames)}
    return {
        "schema_version": 1,
        "start": start,
        "end": end,
        "fps": scene.render.fps / scene.render.fps_base,
        "step": step,
        "frames": frames,
        "tracks": tracks,
        "deforming": astra_deforming_objects(scene, moving),
        "restored_frame": original,
        "limits": "World matrices of evaluated instances only; deforming meshes, particles and "
        "simulations keep their current-frame shape in the preview.",
    }
