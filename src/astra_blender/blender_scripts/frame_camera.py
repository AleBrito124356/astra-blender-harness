"""Trusted camera fitting operation; approval is required by the harness."""

import bpy
from mathutils import Matrix, Vector


def astra_frame_camera(names, margin=0.12, frames=None):
    scene = bpy.context.scene
    if not names:
        raise ValueError(
            "Choose subject object names from astra_inspect_scene; exclude the floor/background."
        )
    missing = [name for name in names if scene.objects.get(name) is None]
    if missing:
        raise ValueError("Unknown subject objects: " + ", ".join(missing))
    objects = [scene.objects[name] for name in names]
    if any(obj.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"} for obj in objects):
        raise ValueError("Select geometry objects only.")
    original_frame = scene.frame_current
    corners = []
    try:
        for frame in frames or [original_frame]:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            corners.extend(
                [
                    instance.matrix_world @ Vector(corner)
                    for instance in depsgraph.object_instances
                    if instance.object.original.name in names
                    for corner in instance.object.bound_box
                ]
            )
    finally:
        scene.frame_set(original_frame)
    if not corners:
        raise ValueError("Selected subjects are not visible in the current view layer.")
    center = Vector([(min(p[i] for p in corners) + max(p[i] for p in corners)) / 2 for i in range(3)])
    radius = max((point - center).length for point in corners)
    camera = scene.camera
    if not camera:
        data = bpy.data.cameras.new("Astra Camera")
        camera = bpy.data.objects.new("Astra Camera", data)
        scene.collection.objects.link(camera)
        camera.location = center + Vector((1.3, -2, 1.2)) * max(radius, 1)
        scene.camera = camera
    if camera.data.type not in {"PERSP", "ORTHO"}:
        raise ValueError("Camera fitting supports perspective and orthographic cameras.")
    if camera.animation_data:
        raise ValueError("Camera has animation data; adjust its keys or use an unanimated camera explicitly.")
    if camera.constraints:
        raise ValueError("Camera has constraints; fit an unconstrained camera or adjust the rig explicitly.")
    bpy.context.view_layer.update()
    direction = camera.matrix_world.translation - center
    if direction.length < 0.001:
        direction = Vector((1.3, -2, 1.2))
    direction.normalize()
    rotation = (-direction).to_track_quat("-Z", "Y")
    camera.data.shift_x = camera.data.shift_y = 0
    if camera.data.type not in {"PERSP", "ORTHO"}:
        raise ValueError("Camera fitting supports perspective and orthographic cameras.")
    low, high = 0.01, max(radius * 4, 1)

    def fits(distance):
        camera.matrix_world = Matrix.LocRotScale(center + direction * distance, rotation, Vector((1, 1, 1)))
        if camera.data.type == "ORTHO":
            camera.data.ortho_scale = distance
        camera.data.clip_end = max(camera.data.clip_end, distance + radius * 3)
        bpy.context.view_layer.update()
        points = [astra_project(scene, camera, point) for point in corners]  # noqa: F821 - supplied by the trusted script builder
        return all(
            camera.data.clip_start < p.z < camera.data.clip_end
            and margin <= p.x <= 1 - margin
            and margin <= p.y <= 1 - margin
            for p in points
        )

    for _ in range(24):
        if fits(high):
            break
        high *= 2
    else:
        raise ValueError("Could not frame the selected subjects.")
    for _ in range(28):
        middle = (low + high) / 2
        if fits(middle):
            high = middle
        else:
            low = middle
    fits(high * 1.002)
    return {
        "camera": camera.name,
        "subjects": names,
        "margin": margin,
        "message": "Subject bounding boxes fit inside the camera margin. Occlusion still needs review.",
    }
