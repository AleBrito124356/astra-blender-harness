"""Trusted assembly operations: validate first, preserve world transforms."""

import bpy
from mathutils import Matrix, Vector


def astra_assemble(root_name, names, anchor, max_gap=0.15):
    scene = bpy.context.scene
    if anchor not in names or root_name in names:
        raise ValueError("Include the anchor among parts; the root must be separate.")
    if len(set(names)) != len(names) or any(scene.objects.get(n) is None for n in names):
        raise ValueError("Parts must be distinct, existing scene objects.")
    parts = [scene.objects[n] for n in names]
    root = scene.objects.get(root_name)
    if root and (root.type != "EMPTY" or root.parent or root.constraints or root.animation_data):
        raise ValueError("Use a new root name or an unparented, unanimated Empty.")
    if any(p.animation_data or p.constraints for p in parts):
        raise ValueError("Assemble before animation/constraints, or adapt the existing rig explicitly.")
    for part in parts:
        if root and root in part.children_recursive:
            raise ValueError("Assembly would create a parenting cycle.")
    if not root:
        root = bpy.data.objects.new(root_name, None)
        scene.collection.objects.link(root)
        root.location = scene.objects[anchor].matrix_world.translation
    bpy.context.view_layer.update()
    worlds = [p.matrix_world.copy() for p in parts]
    for part, world in zip(parts, worlds):
        part.parent = root
        part.matrix_parent_inverse = Matrix.Identity(4)
        part.matrix_world = world
        part["astra_anchor"] = anchor
        part["astra_max_gap"] = max_gap
    root["astra_role"] = "assembly"
    bpy.context.view_layer.update()
    return {
        "root": root.name,
        "anchor": anchor,
        "parts": names,
        "message": "World transforms preserved. Check attachment gaps before adding keys.",
    }


def astra_ground(root_name, ground_name, clearance=0.0):
    scene = bpy.context.scene
    root, ground = scene.objects.get(root_name), scene.objects.get(ground_name)
    if not root or not ground or root == ground:
        raise ValueError("Choose an existing assembly root and a separate horizontal ground object.")
    if root.constraints or root.animation_data:
        raise ValueError("Ground the assembly before animating or constraining its root.")
    members = [root] + list(root.children_recursive)
    if ground in members or ground.type != "MESH":
        raise ValueError("Ground must be a separate mesh.")
    if abs((ground.matrix_world.to_quaternion() @ Vector((0, 0, 1))).z) < 0.999:
        raise ValueError("This tool supports horizontal ground only.")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    names = [p.name for p in members]
    points = [
        i.matrix_world @ Vector(v)
        for i in depsgraph.object_instances
        if i.object.original.name in names and i.object.type == "MESH"
        for v in i.object.bound_box
    ]
    if not points:
        raise ValueError("Assembly has no evaluated mesh geometry.")
    floor = ground.evaluated_get(depsgraph)
    floor_points = [floor.matrix_world @ Vector(v) for v in floor.bound_box]
    low, high = min(v.z for v in points), max(v.z for v in floor_points)
    world = root.matrix_world.copy()
    world.translation.z += high + clearance - low
    root.matrix_world = world
    bpy.context.view_layer.update()
    return {
        "root": root.name,
        "ground": ground.name,
        "offset_z": high + clearance - low,
        "message": "Assembly translated as a unit. Placement uses horizontal ground bounds, not a physics simulation.",
    }
