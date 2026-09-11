"""Trusted read-only bpy probe. Executed inside Blender, never imported by the server."""

import hashlib
import math
from array import array

import bpy
from mathutils import Vector


def _numbers(values):
    return [round(float(v), 5) if math.isfinite(float(v)) else 0.0 for v in values]


def _material(mat):
    base = [0.55, 0.58, 0.62, 1]
    data = {
        "name": mat.name if mat else "Default",
        "color": base[:3],
        "roughness": 0.5,
        "metallic": 0,
        "opacity": 1,
    }
    if mat:
        base = list(mat.diffuse_color)
        data["color"], data["opacity"] = base[:3], base[3]
        if mat.use_nodes:
            shader = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if shader:
                for socket, key in [
                    ("Base Color", "color"),
                    ("Roughness", "roughness"),
                    ("Metallic", "metallic"),
                    ("Alpha", "opacity"),
                ]:
                    value = shader.inputs.get(socket)
                    if value:
                        data[key] = (
                            list(value.default_value)[:3] if key == "color" else float(value.default_value)
                        )
                transmission = shader.inputs.get("Transmission Weight") or shader.inputs.get("Transmission")
                if transmission and transmission.default_value > 0.1:
                    data["opacity"] = min(data["opacity"], 0.35)
    return data


def astra_action_curves(obj):
    """F-curves of the object's action, including Blender 4.4+ layered actions."""
    data = obj.animation_data
    if not data or not data.action:
        return []
    action = data.action
    if hasattr(action, "layers") and action.layers:
        curves, slot = [], data.action_slot
        if slot:
            for layer in action.layers:
                for strip in layer.strips:
                    if strip.type == "KEYFRAME":
                        bag = strip.channelbag(slot)
                        if bag:
                            curves.extend(list(bag.fcurves))
        return curves
    return list(action.fcurves) if hasattr(action, "fcurves") else []


def astra_animation_fingerprint(animated):
    digest = hashlib.sha1()
    for obj in animated:
        digest.update(obj.name.encode())
        for curve in astra_action_curves(obj):
            digest.update(f"{curve.data_path}[{curve.array_index}]".encode())
            for point in curve.keyframe_points:
                digest.update(f"{point.co.x:.3f}:{point.co.y:.5f}:{point.interpolation}".encode())
    return digest.hexdigest()[:16] if animated else ""


def astra_geometry_hash(obj, materials):
    """Cheap signature of an evaluated mesh and its materials, or None when the
    type has no fast path and must be serialized every time."""
    data = obj.data
    if obj.type != "MESH" or data is None or not hasattr(data, "vertices"):
        return None
    digest = hashlib.sha1()
    digest.update(
        repr(
            [(m["name"], m["color"], m["roughness"], m["metallic"], m["opacity"]) for m in materials]
        ).encode()
    )
    count = len(data.vertices)
    coords = array("f", [0.0]) * (count * 3)
    if count:
        data.vertices.foreach_get("co", coords)
    digest.update(coords.tobytes())
    digest.update(f"{count}:{len(data.polygons)}".encode())
    return digest.hexdigest()


def astra_scene_probe(geometry=False, known=None):
    # known maps instance key to the geometry hash the caller already holds;
    # matching meshes are reported without positions, normals or triangles.
    known = known or {}
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    camera = scene.camera
    objects, meshes, warnings = [], [], []
    vertex_budget, triangle_budget = 45000, 60000
    occurrences = {}
    for index, instance in enumerate(depsgraph.object_instances):
        obj = instance.object
        if obj.hide_render or obj.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
            continue
        # Same key the motion bake uses, so baked tracks find their meshes:
        # the object name, then name#2, name#3 for further instances.
        occurrences[obj.name] = occurrences.get(obj.name, 0) + 1
        key = obj.name if occurrences[obj.name] == 1 else f"{obj.name}#{occurrences[obj.name]}"
        if len(objects) >= 250:
            warnings.append("Scene preview limited to 250 visible objects/instances.")
            break
        matrix = instance.matrix_world.copy()
        corners = [matrix @ Vector(corner) for corner in obj.bound_box]
        low = [min(p[i] for p in corners) for i in range(3)]
        high = [max(p[i] for p in corners) for i in range(3)]
        center = (Vector(low) + Vector(high)) / 2
        dimensions = [high[i] - low[i] for i in range(3)]
        record = {
            "id": str(index) + ":" + obj.name,
            "key": key,
            "name": obj.name,
            "type": obj.type,
            "parent": obj.original.parent.name if obj.original.parent else None,
            "anchor": obj.original.get("astra_anchor", ""),
            "max_gap": obj.original.get("astra_max_gap", 0.15),
            "origin": _numbers(matrix.translation),
            "bounds": [_numbers(low), _numbers(high)],
            "center": _numbers(center),
            "dimensions": _numbers(dimensions),
            "materials": [slot.material.name for slot in obj.material_slots if slot.material],
        }
        if camera and camera.data.type in {"PERSP", "ORTHO"}:
            projected = [astra_project(scene, camera, p) for p in corners]  # noqa: F821 - supplied by the trusted script builder
            projected_center = astra_project(scene, camera, center)  # noqa: F821 - supplied by the trusted script builder
            record["camera"] = {
                "center": _numbers(projected_center),
                "bounds": [
                    _numbers([min(p.x for p in projected), min(p.y for p in projected)]),
                    _numbers([max(p.x for p in projected), max(p.y for p in projected)]),
                ],
                "corners_in_frame": sum(
                    camera.data.clip_start < p.z < camera.data.clip_end and 0 <= p.x <= 1 and 0 <= p.y <= 1
                    for p in projected
                ),
                "center_in_frame": bool(
                    projected_center.z > 0 and 0 <= projected_center.x <= 1 and 0 <= projected_center.y <= 1
                ),
                "behind_camera": all(p.z <= 0 for p in projected),
            }
        objects.append(record)
        if not geometry:
            continue
        entry = {
            **record,
            "matrix": _numbers([matrix[r][c] for c in range(4) for r in range(4)]),
            "materials": [_material(slot.material) for slot in obj.material_slots] or [_material(None)],
        }
        entry["hash"] = astra_geometry_hash(obj, entry["materials"])
        if entry["hash"] and known.get(key) == entry["hash"]:
            entry["unchanged"] = True
            meshes.append(entry)
            continue
        mesh = None
        try:
            mesh = obj.to_mesh()
            if not mesh or not mesh.vertices:
                continue
            if len(mesh.vertices) > vertex_budget or len(mesh.polygons) * 2 > triangle_budget:
                entry["proxy"] = True
            else:
                mesh.calc_loop_triangles()
                if len(mesh.loop_triangles) > triangle_budget:
                    entry["proxy"] = True
                else:
                    entry["positions"] = _numbers([v for vertex in mesh.vertices for v in vertex.co])
                    entry["triangles"] = [v for tri in mesh.loop_triangles for v in tri.vertices]
                    entry["material_indices"] = [tri.material_index for tri in mesh.loop_triangles]
                    entry["normals"] = _numbers(
                        [
                            value
                            for tri in mesh.loop_triangles
                            for loop in tri.loops
                            for value in mesh.corner_normals[loop].vector
                        ]
                    )
                    vertex_budget -= len(mesh.vertices)
                    triangle_budget -= len(mesh.loop_triangles)
            meshes.append(entry)
        finally:
            if mesh:
                obj.to_mesh_clear()
    if any(mesh.get("proxy") for mesh in meshes):
        warnings.append("Dense objects are shown as bounding-box proxies to keep Blender responsive.")
    camera_info = None
    if camera:
        if camera.data.type not in {"PERSP", "ORTHO"}:
            warnings.append(
                "Camera projection checks and browser camera view require perspective or orthographic mode."
            )
        quaternion = camera.matrix_world.to_quaternion()
        camera_info = {
            "name": camera.name,
            "position": _numbers(camera.matrix_world.translation),
            "quaternion": _numbers(quaternion[1:]) + [round(quaternion[0], 6)],
            "type": camera.data.type,
            "lens": camera.data.lens,
            "fov": math.degrees(camera.data.angle_y),
            "ortho_scale": camera.data.ortho_scale,
            "shift": [camera.data.shift_x, camera.data.shift_y],
            "clip": [camera.data.clip_start, camera.data.clip_end],
            "view_frame": [_numbers(v) for v in camera.data.view_frame(scene=scene)],
        }
    animated = [o for o in scene.objects if o.animation_data and o.animation_data.action]
    sockets = []
    for mat in bpy.data.materials:
        if mat.use_nodes:
            shader = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if shader:
                sockets = [socket.name for socket in shader.inputs]
                break
    return {
        "schema_version": 1,
        "scene": scene.name,
        "source_file": bpy.path.basename(bpy.data.filepath) if bpy.data.filepath else "Unsaved scene",
        "frame": scene.frame_current,
        "timeline": {
            "start": scene.frame_start,
            "end": scene.frame_end,
            "fps": scene.render.fps / scene.render.fps_base,
            "animated_objects": [o.name for o in animated],
            # Changes whenever keyframes change, so a baked preview knows it is stale.
            "fingerprint": astra_animation_fingerprint(animated),
        },
        "blender_version": bpy.app.version_string,
        "objects": objects,
        "meshes": meshes,
        "camera": camera_info,
        "warnings": warnings,
        "render": {
            "engine": scene.render.engine,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "percentage": scene.render.resolution_percentage,
            "pixel_aspect": [scene.render.pixel_aspect_x, scene.render.pixel_aspect_y],
        },
        "capabilities": {
            "principled_inputs": sockets,
            "view_settings_location": "bpy.context.scene.view_settings",
            "collection_membership": "object.users_collection (not object.data.collections)",
            "eevee_properties": [p.identifier for p in scene.eevee.bl_rna.properties]
            if hasattr(scene, "eevee")
            else [],
        },
    }
