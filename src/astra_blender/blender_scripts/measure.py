"""Calibrated measurement helpers for trusted scripts (list 'measure.py' in a spec).

Ported from the prototypes verified headless on Blender 5.2.1: the camera
matrix and ray grid (blender-facts p_a/p_b, perception sight_view), the
blind light meter with its measured photometric constants (p_f), world-space
BVH trees and support rays (qa-first facts_probe). scripts.build places this
file right after common.py. Safe-mode clean: one def per helper, no lambda.

WS1 owns this file after the foundation and may add helpers, but keeps these
signatures; WS2 and WS3 only call them. projection.py's astra_project(scene,
camera, point) stays the per-point API; astra_project_uv is the fast variant
over a precomputed matrix, named so it never shadows projection.py when both
files are concatenated.
"""

import math

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

ASTRA_SYMBOLS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
# A 0.18 grey card lit by irradiance 1.0 renders at linear 0.18: 0 EV.
ASTRA_GREY = 0.18


def astra_camera_matrix(scene, camera, depsgraph=None):
    """Projection x view matrix of a camera, as the renderer frames it."""
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    render = scene.render
    projection = camera.calc_matrix_camera(
        depsgraph,
        x=render.resolution_x,
        y=render.resolution_y,
        scale_x=render.pixel_aspect_x,
        scale_y=render.pixel_aspect_y,
    )
    return projection @ camera.matrix_world.normalized().inverted()


def astra_project_uv(matrix, point):
    """(u, v, behind) of a world point: u, v in 0..1 across the frame, v up.

    behind is True when the point is not in front of the near clip plane;
    its u, v are then mirrored, as world_to_camera_view's are, and should be
    ignored. Matches bpy_extras.world_to_camera_view to 4 decimals.
    """
    clip = matrix @ Vector((point[0], point[1], point[2], 1.0))
    if abs(clip.w) < 1e-9:
        return 0.5, 0.5, True
    behind = clip.w < 0.0 or clip.z / clip.w < -1.0
    return clip.x / clip.w * 0.5 + 0.5, clip.y / clip.w * 0.5 + 0.5, behind


def astra_camera_frame(scene, camera):
    """(tr, br, bl, tl, origin, forward, ortho) of the camera in world space."""
    world = camera.matrix_world.normalized()
    tr, br, bl, tl = [world @ corner for corner in camera.data.view_frame(scene=scene)]
    forward = (world.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()
    return tr, br, bl, tl, world.translation.copy(), forward, camera.data.type == "ORTHO"


def astra_camera_ray(frame, u, v, clip_start):
    """(start, direction) of the camera ray through frame point u, v (v from the top)."""
    tr, br, bl, tl, origin, forward, ortho = frame
    point = tl.lerp(bl, v).lerp(tr.lerp(br, v), u)
    if ortho:
        return point - forward * clip_start, forward
    return origin, (point - origin).normalized()


def astra_ray_grid(w, h, layers=1, budget_s=None):
    """Depth-peeled ray grid from the render camera: who is seen, who hides whom.

    Each of w x h rays (row 0 at the top) records up to `layers` distinct
    objects along its path. Returns {w, h, rows (one symbol per cell, '.' for
    the world), legend {name: symbol}, objects {name: {first, any, zsum,
    zmin, back, u0, u1, v0, v1, su, sv, by {occluder: cells}}}, hits
    [(name, location, normal, direction)] for first hits, void, rays,
    truncated}. first counts cells where the object is the nearest hit, any
    cells where it lies anywhere on the ray; by names what was in front.
    v in the stats runs from the top. Stops early, with truncated set, when
    budget_s seconds of the uuid1 clock have passed.
    """
    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        raise ValueError("No active camera. Set scene.camera or create one with a camera tool.")
    if camera.data.type not in {"PERSP", "ORTHO"}:
        raise ValueError("Only perspective and orthographic cameras can be ray-cast.")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    frame = astra_camera_frame(scene, camera)
    clip_start, clip_end = camera.data.clip_start, camera.data.clip_end
    t0 = astra_clock()  # noqa: F821 - common.py precedes this file
    legend, stats, rows, hits = {}, {}, [], []
    rays = 0
    truncated = False
    for j in range(h):
        if budget_s is not None and astra_budget_left(t0, budget_s) <= 0:  # noqa: F821
            truncated = True
            break
        v = (j + 0.5) / h
        line = []
        for i in range(w):
            u = (i + 0.5) / w
            start, direction = astra_camera_ray(frame, u, v, clip_start)
            first, count = astra_peel(scene, depsgraph, start, direction, clip_end, layers, legend, stats, hits)
            rays += count
            if first is not None:
                astra_grid_mark(stats[first], u, v, frame[4], frame[5], hits[-1])
            line.append(legend[first] if first is not None else ".")
        rows.append("".join(line))
    cells = max(1, w * len(rows))
    void = sum(row.count(".") for row in rows) / cells
    return {
        "w": w,
        "h": h,
        "rows": rows,
        "legend": legend,
        "objects": stats,
        "hits": hits,
        "void": void,
        "rays": rays,
        "truncated": truncated,
    }


def astra_peel(scene, depsgraph, start, direction, clip_end, layers, legend, stats, hits):
    """Walk one ray through up to `layers` distinct objects. Returns (first name, casts)."""
    seen = []
    cursor = start
    travelled = 0.0
    first = None
    casts = 0
    for _step in range(layers * 3):
        if travelled >= clip_end:
            break
        casts += 1
        hit, location, normal, _index, obj, _matrix = scene.ray_cast(
            depsgraph, cursor, direction, distance=clip_end - travelled
        )
        if not hit:
            break
        name = obj.original.name
        travelled += (location - cursor).length + 1e-4
        cursor = location + direction * 1e-4
        if name in seen:
            continue
        seen.append(name)
        if name not in stats:
            legend[name] = ASTRA_SYMBOLS[len(legend) % len(ASTRA_SYMBOLS)]
            stats[name] = {
                "first": 0, "any": 0, "zsum": 0.0, "zmin": 1e30, "back": 0,
                "u0": 1.0, "u1": 0.0, "v0": 1.0, "v1": 0.0, "su": 0.0, "sv": 0.0, "by": {},
            }
        record = stats[name]
        record["any"] += 1
        if first is None:
            first = name
            hits.append((name, location.copy(), normal.copy(), direction.copy()))
        else:
            record["by"][first] = record["by"].get(first, 0) + 1
        if len(seen) >= layers:
            break
    return first, casts


def astra_grid_mark(record, u, v, origin, forward, hit):
    """Fold one first-hit cell into an object's grid statistics."""
    _name, location, normal, direction = hit
    depth = (location - origin).dot(forward)
    record["first"] += 1
    record["zsum"] += depth
    record["zmin"] = min(record["zmin"], depth)
    record["u0"] = min(record["u0"], u)
    record["u1"] = max(record["u1"], u)
    record["v0"] = min(record["v0"], v)
    record["v1"] = max(record["v1"], v)
    record["su"] += u
    record["sv"] += 1.0 - v
    if normal.dot(direction) > 0:
        record["back"] += 1


def astra_luma(color):
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


def astra_lights(scene):
    """Render-visible lights with their calibrated power (energy x 2^exposure x luma)."""
    lights = []
    for obj in scene.objects:
        if obj.type != "LIGHT" or obj.hide_render:
            continue
        data = obj.data
        color = Vector(data.color)
        if data.use_temperature:
            tint = data.temperature_color
            color = Vector((color[0] * tint[0], color[1] * tint[1], color[2] * tint[2]))
        lights.append({
            "name": obj.name,
            "type": data.type,
            "power": float(data.energy) * (2.0 ** float(data.exposure)) * astra_luma(color),
            "pos": obj.matrix_world.translation.copy(),
            "axis": (obj.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized(),
            "spot_size": data.spot_size if data.type == "SPOT" else 0.0,
            "spot_blend": data.spot_blend if data.type == "SPOT" else 0.0,
        })
    return lights


def astra_world_ambient(scene):
    """Uniform world light as linear luminance (colour x strength), None if textured."""
    world = scene.world
    if world is None:
        return 0.0
    if world.node_tree is None:
        return astra_luma(world.color)
    for node in world.node_tree.nodes:
        if node.type == "BACKGROUND":
            if node.inputs["Color"].is_linked:
                return None
            return astra_luma(node.inputs["Color"].default_value) * float(node.inputs["Strength"].default_value)
    return None


def astra_light_irradiance(light, point):
    """(irradiance at normal incidence, direction to the light, distance).

    Measured 5.2.1 constants: SUN S/pi; POINT and SPOT P/(4 pi^2 d^2), with
    the spot cone and blend; AREA P/(pi^2 d^2) x its facing cosine.
    """
    kind = light["type"]
    if kind == "SUN":
        return light["power"] / math.pi, -light["axis"], 1.0e5
    delta = light["pos"] - point
    distance = max(1e-4, delta.length)
    direction = delta / distance
    if kind == "AREA":
        facing = max(0.0, light["axis"].dot(-direction))
        return light["power"] / (math.pi * math.pi * distance * distance) * facing, direction, distance
    value = light["power"] / (4.0 * math.pi * math.pi * distance * distance)
    if kind == "SPOT":
        angle = math.acos(max(-1.0, min(1.0, light["axis"].dot(-direction))))
        half = light["spot_size"] / 2.0
        inner = half * (1.0 - light["spot_blend"])
        if angle > half:
            value = 0.0
        elif angle > inner:
            x = (angle - inner) / max(1e-6, half - inner)
            value *= 1.0 - x * x * (3.0 - 2.0 * x)
    return value, direction, distance


def astra_subject_samples(scene, depsgraph, subject_objs, samples):
    """Up to `samples` (point, normal) pairs on the subjects' camera-facing surface.

    Rays go through a grid over the subjects' projected bounds and hit only
    the subjects (object-level ray_cast), so an occluder in front does not
    move the samples. Without a camera, a viewpoint above-front-left of the
    subjects is used.
    """
    lo, hi = astra_world_box(subject_objs)  # noqa: F821 - common.py precedes this file
    center = (lo + hi) / 2
    radius = max(0.05, (hi - lo).length / 2)
    camera = scene.camera
    side = max(4, int(math.ceil(math.sqrt(samples))) * 2)
    rays = []
    if camera is not None and camera.data.type in {"PERSP", "ORTHO"}:
        frame = astra_camera_frame(scene, camera)
        matrix = astra_camera_matrix(scene, camera, depsgraph)
        us, vs = [], []
        for x in (lo.x, hi.x):
            for y in (lo.y, hi.y):
                for z in (lo.z, hi.z):
                    u, v, behind = astra_project_uv(matrix, (x, y, z))
                    if not behind:
                        us.append(u)
                        vs.append(1.0 - v)
        if us:
            u0, u1 = max(0.0, min(us)), min(1.0, max(us))
            v0, v1 = max(0.0, min(vs)), min(1.0, max(vs))
            for j in range(side):
                for i in range(side):
                    u = u0 + (u1 - u0) * (i + 0.5) / side
                    v = v0 + (v1 - v0) * (j + 0.5) / side
                    rays.append(astra_camera_ray(frame, u, v, camera.data.clip_start))
    if not rays:
        eye = center + Vector((-1.0, -1.0, 0.8)).normalized() * radius * 4.0
        forward = (center - eye).normalized()
        right = forward.cross(Vector((0.0, 0.0, 1.0))).normalized()
        up = right.cross(forward).normalized()
        for j in range(side):
            for i in range(side):
                offset = right * ((i + 0.5) / side * 2.0 - 1.0) + up * (1.0 - (j + 0.5) / side * 2.0)
                target = center + offset * radius
                rays.append((eye, (target - eye).normalized()))
    found = []
    for start, direction in rays:
        best = None
        for obj in subject_objs:
            hit = astra_object_hit(obj, depsgraph, start, direction)
            if hit is not None and (best is None or hit[0] < best[0]):
                best = hit
        if best is not None:
            normal = best[2] if best[2].dot(direction) < 0 else -best[2]
            found.append((best[1], normal))
    stride = max(1, len(found) // max(1, samples))
    return found[::stride][:samples]


def astra_object_hit(obj, depsgraph, start, direction):
    """(distance, world point, world normal) where a world ray hits obj, or None."""
    if obj.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
        return None
    world = obj.matrix_world
    inverse = world.inverted_safe()
    local_start = inverse @ start
    local_direction = (inverse.to_3x3() @ direction).normalized()
    try:
        hit, location, normal, _index = obj.ray_cast(local_start, local_direction, depsgraph=depsgraph)
    except RuntimeError:
        # Objects without evaluated mesh data (an empty curve) cannot be ray-cast.
        return None
    if not hit:
        return None
    point = world @ location
    world_normal = (world.to_3x3().inverted_safe().transposed() @ normal).normalized()
    return (point - start).length, point, world_normal


def astra_meter(subject_objs, samples=128):
    """Blind light meter: how bright a 0.18 grey subject renders, without rendering.

    Averages direct light over `samples` surface points (at least 100 are
    needed for complex shapes) with shadow rays, plus the uniform world.
    Errors against Cycles direct lighting were -3 to +2 %. Returns {subject,
    samples, irradiance, ev (0 = mid grey, view exposure included), key_fill,
    ambient_share, lights [{name, e, shadowed?, shadowed_by?}]} with lights
    sorted brightest first; ev is None without samples.
    """
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    subjects = list(subject_objs)
    lights = astra_lights(scene)
    ambient = astra_world_ambient(scene) or 0.0
    points = astra_subject_samples(scene, depsgraph, subjects, max(1, int(samples)))
    lit = [0.0 for _ in lights]
    blocked = [0 for _ in lights]
    blockers = [{} for _ in lights]
    ambient_sum = 0.0
    for point, normal in points:
        ambient_sum += ambient * (0.5 + 0.5 * max(-1.0, min(1.0, normal.z)))
        for index, light in enumerate(lights):
            value, direction, distance = astra_light_irradiance(light, point)
            cosine = normal.dot(direction)
            if cosine <= 0.0 or value <= 0.0:
                continue
            hit, _p, _n, _i, blocker, _m = scene.ray_cast(
                depsgraph, point + normal * 1e-3, direction, distance=distance - 2e-3
            )
            if hit:
                blocked[index] += 1
                name = blocker.original.name
                blockers[index][name] = blockers[index].get(name, 0) + 1
            else:
                lit[index] += value * cosine
    count = len(points)
    result = {"subject": [obj.name for obj in subjects], "samples": count}
    if not count:
        result.update({"irradiance": 0.0, "ev": None, "key_fill": None, "ambient_share": 0.0, "lights": []})
        return result
    total = (ambient_sum + sum(lit)) / count
    per_light = []
    for index, light in enumerate(lights):
        entry = {"name": light["name"], "e": round(lit[index] / count, 5)}
        if blocked[index]:
            entry["shadowed"] = round(blocked[index] / count, 3)
            entry["shadowed_by"] = astra_top_key(blockers[index])
        per_light.append(entry)
    per_light.sort(key=astra_light_e, reverse=True)
    key_fill = None
    if len(per_light) > 1 and per_light[1]["e"] > 1e-9:
        key_fill = round(per_light[0]["e"] / per_light[1]["e"], 3)
    result.update({
        "irradiance": round(total, 5),
        "ev": round(math.log2(max(1e-6, total)) + scene.view_settings.exposure, 3),
        "key_fill": key_fill,
        "ambient_share": round(ambient_sum / count / max(1e-9, total), 3),
        "lights": per_light,
    })
    return result


def astra_light_e(entry):
    return entry["e"]


def astra_top_key(counts):
    best, most = None, -1
    for name, value in counts.items():
        if value > most:
            best, most = name, value
    return best


def astra_world_bvh(obj, depsgraph=None):
    """A BVHTree of obj's evaluated mesh in world space, or None without faces.

    Built with FromPolygons on world-space vertices: BVHTree.FromObject is
    object-local, so overlap tests between two objects gave false hits.
    """
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        world = evaluated.matrix_world
        vertices = [world @ vertex.co for vertex in mesh.vertices]
        polygons = [tuple(polygon.vertices) for polygon in mesh.polygons]
    finally:
        evaluated.to_mesh_clear()
    if not polygons:
        return None
    return BVHTree.FromPolygons(vertices, polygons)


def astra_support_ray(obj, candidates, depsgraph=None):
    """What obj rests on: {on, gap_m} from downward rays over its footprint.

    Five rays (centre and four points at 30 % of the footprint) start just
    above obj and hit only each candidate (object-level ray_cast in the
    candidate's space). The highest surface not above obj's middle is the
    support; gap_m = obj bottom - support top, negative when sunk. {on: None,
    gap_m: None} when nothing is below.
    """
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    lo, hi = astra_world_box([obj])  # noqa: F821 - common.py precedes this file
    dx, dy = (hi.x - lo.x) * 0.3, (hi.y - lo.y) * 0.3
    cx, cy = (lo.x + hi.x) / 2, (lo.y + hi.y) / 2
    ceiling = lo.z + max(0.02, 0.5 * (hi.z - lo.z))
    best, support = None, None
    down = Vector((0.0, 0.0, -1.0))
    for candidate in candidates:
        if candidate.name == obj.name or candidate.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
            continue
        clo, chi = astra_world_box([candidate])  # noqa: F821
        if chi.x < lo.x or clo.x > hi.x or chi.y < lo.y or clo.y > hi.y or clo.z > hi.z:
            continue
        for ox, oy in ((0.0, 0.0), (dx, dy), (-dx, dy), (dx, -dy), (-dx, -dy)):
            start = Vector((cx + ox, cy + oy, hi.z + 1e-3))
            hit = astra_object_hit(candidate, depsgraph, start, down)
            if hit is None or hit[1].z > ceiling:
                continue
            if best is None or hit[1].z > best:
                best, support = hit[1].z, candidate.name
    if best is None:
        return {"on": None, "gap_m": None}
    return {"on": support, "gap_m": round(lo.z - best, 5)}
