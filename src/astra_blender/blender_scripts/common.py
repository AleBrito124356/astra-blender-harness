"""Helpers prepended to every 0.4 trusted script (scripts.build, common=True).

Runs inside Blender under upstream safe mode: bpy, bmesh, mathutils and the
stdlib allowlist only; one def per helper; no lambda, class, computed getattr
or dir(). Verified on Blender 5.2.1 by tests/test_blender_helpers.py.
"""

import math
import uuid

import bpy
from mathutils import Vector

ASTRA_GEOMETRY = {"MESH", "CURVE", "SURFACE", "FONT", "META"}
# Types whose render geometry the depsgraph also yields as a MESH instance of
# the object itself; counting the original too would list them twice.
ASTRA_CONVERTED = {"CURVE", "SURFACE", "FONT", "META"}
ASTRA_RESTORE_KEY = "astra_restore"


def astra_clock():
    """Seconds on the only clock safe mode allows (uuid1 counts 100 ns steps)."""
    return uuid.uuid1().time / 1e7


def astra_budget_left(t0, budget_s):
    """Seconds left of budget_s since astra_clock() returned t0 (may be negative)."""
    return float(budget_s) - (astra_clock() - t0)


def astra_update():
    """Refresh matrix_world and evaluated data after edits, before measuring."""
    bpy.context.view_layer.update()


def astra_require(names):
    """The named scene objects, or ValueError naming every unknown one.

    Call it before changing anything: Blender does not roll back a script
    that fails halfway.
    """
    scene = bpy.context.scene
    missing = [name for name in names if scene.objects.get(name) is None]
    if missing:
        shown = ", ".join(missing[:12]) + (" and %d more" % (len(missing) - 12) if len(missing) > 12 else "")
        raise ValueError("Unknown objects: " + shown)
    return [scene.objects[name] for name in names]


def astra_is_astra(obj):
    """True for objects Astra created or tagged (astra_kind or astra_role)."""
    return obj.get("astra_kind") is not None or obj.get("astra_role") is not None


def astra_world_box(objs):
    """Evaluated world AABB (lo, hi) of the objects and their geometry children.

    Without any geometry it falls back to the object origins.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    lo = Vector((1e18, 1e18, 1e18))
    hi = Vector((-1e18, -1e18, -1e18))
    found = False
    queue = list(objs)
    seen = set()
    while queue:
        obj = queue.pop()
        if obj.name in seen:
            continue
        seen.add(obj.name)
        queue.extend(obj.children)
        if obj.type not in ASTRA_GEOMETRY:
            continue
        evaluated = obj.evaluated_get(depsgraph)
        for corner in evaluated.bound_box:
            point = evaluated.matrix_world @ Vector(corner)
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
            found = True
    if not found:
        for obj in objs:
            point = obj.matrix_world.translation
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
    return lo, hi


def astra_fcurves(idblock, ensure=False):
    """The F-curve collection of an ID's action on Blender 5.x layered actions.

    action.fcurves no longer exists; the curves live in
    action.layers[0].strips[0].channelbag(slot). With ensure=True the
    animation data, action, slot, layer, strip and channelbag are created as
    needed; otherwise an unanimated ID gives an empty list.
    """
    animation = idblock.animation_data
    if animation is None:
        if not ensure:
            return []
        animation = idblock.animation_data_create()
    action = animation.action
    if action is None:
        if not ensure:
            return []
        action = bpy.data.actions.new(idblock.name + "Action")
        animation.action = action
    slot = animation.action_slot
    if slot is None:
        if not ensure:
            return []
        slot = action.slots.new(id_type=idblock.id_type, name=idblock.name)
        animation.action_slot = slot
    if len(action.layers) == 0:
        if not ensure:
            return []
        action.layers.new("Layer")
    layer = action.layers[0]
    if len(layer.strips) == 0:
        if not ensure:
            return []
        layer.strips.new(type="KEYFRAME")
    strip = layer.strips[0]
    bag = strip.channelbag(slot, ensure=ensure)
    if bag is None:
        return []
    return bag.fcurves


def astra_geometry(depsgraph, include_hidden=False):
    """Every render-visible geometry, counted once, in one depsgraph pass.

    Returns [{name, type, kind: 'object'|'instancer', lo, hi, matrix,
    evaluated, instances, sources}]:
    - Curve, text, metaball and surface objects are counted once, through the
      MESH instance the depsgraph makes of their render geometry, and are
      named and typed after the original object.
    - Collection and Geometry Nodes instances are aggregated per instancer:
      `instances` counts them, `sources` names what they instance, and lo/hi
      grow to include them. The instancer's own geometry, if any, sets
      matrix/evaluated.
    """
    records = {}
    order = []
    for instance in depsgraph.object_instances:
        obj = instance.object
        original = obj.original
        parent = instance.parent.original if instance.is_instance and instance.parent is not None else None
        if instance.is_instance and parent is not None and parent.name != original.name:
            owner = parent
            own = False
        elif instance.is_instance:
            owner = original
            own = True
        else:
            if original.type in ASTRA_CONVERTED or original.type not in ASTRA_GEOMETRY:
                continue
            owner = original
            own = True
        if obj.type not in ASTRA_GEOMETRY:
            continue
        if not include_hidden and (owner.hide_render or original.hide_render):
            continue
        matrix = instance.matrix_world.copy()
        record = records.get(owner.name)
        if record is None:
            record = {
                "name": owner.name,
                "type": owner.type,
                "kind": "object",
                "lo": Vector((1e18, 1e18, 1e18)),
                "hi": Vector((-1e18, -1e18, -1e18)),
                "matrix": None,
                "evaluated": None,
                "instances": 0,
                "sources": [],
            }
            records[owner.name] = record
            order.append(owner.name)
        if own:
            record["matrix"] = matrix
            record["evaluated"] = obj
        else:
            record["kind"] = "instancer"
            record["instances"] += 1
            if original.name not in record["sources"]:
                record["sources"].append(original.name)
        for corner in obj.bound_box:
            point = matrix @ Vector(corner)
            for axis in range(3):
                record["lo"][axis] = min(record["lo"][axis], point[axis])
                record["hi"][axis] = max(record["hi"][axis], point[axis])
    return [records[name] for name in order]


def astra_render_snapshot(scene):
    """Every render and colour setting a look or render tool may touch.

    The snapshot is also written to scene['astra_restore'] first, so a later
    call can put the scene back even if Blender died mid-render.
    """
    render = scene.render
    image = render.image_settings
    view = scene.view_settings
    snapshot = {
        "engine": render.engine,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "filepath": render.filepath,
        "media_type": image.media_type,
        "file_format": image.file_format,
        "color_mode": image.color_mode,
        "color_depth": image.color_depth,
        "eevee_samples": scene.eevee.taa_render_samples,
        "view_transform": view.view_transform,
        "look": view.look,
        "exposure": view.exposure,
        "film_transparent": render.film_transparent,
    }
    if hasattr(scene, "cycles"):
        snapshot["cycles_samples"] = scene.cycles.samples
    scene[ASTRA_RESTORE_KEY] = snapshot
    return snapshot


def astra_render_restore(scene, snapshot=None):
    """Put back an astra_render_snapshot, or the scene's leftover record.

    Returns the names that could not be restored (normally none). The record
    in scene['astra_restore'] is removed once restored.
    """
    if snapshot is None:
        stored = scene.get(ASTRA_RESTORE_KEY)
        if stored is None:
            return []
        snapshot = stored.to_dict()
    render = scene.render
    image = render.image_settings
    view = scene.view_settings
    failed = []
    try:
        render.engine = snapshot["engine"]
    except (KeyError, TypeError, ValueError):
        failed.append("engine")
    render.resolution_x = int(snapshot.get("resolution_x", render.resolution_x))
    render.resolution_y = int(snapshot.get("resolution_y", render.resolution_y))
    render.resolution_percentage = int(snapshot.get("resolution_percentage", render.resolution_percentage))
    render.filepath = str(snapshot.get("filepath", render.filepath))
    render.film_transparent = bool(snapshot.get("film_transparent", render.film_transparent))
    # The order matters: media_type before file_format, the format before its
    # colour mode and depth, the view transform before its look.
    try:
        image.media_type = snapshot["media_type"]
        image.file_format = snapshot["file_format"]
    except (KeyError, TypeError, ValueError):
        failed.append("file_format")
    try:
        image.color_mode = snapshot["color_mode"]
    except (KeyError, TypeError, ValueError):
        failed.append("color_mode")
    try:
        image.color_depth = snapshot["color_depth"]
    except (KeyError, TypeError, ValueError):
        failed.append("color_depth")
    try:
        view.view_transform = snapshot["view_transform"]
        view.look = snapshot["look"]
    except (KeyError, TypeError, ValueError):
        failed.append("look")
    view.exposure = float(snapshot.get("exposure", view.exposure))
    if "eevee_samples" in snapshot:
        scene.eevee.taa_render_samples = int(snapshot["eevee_samples"])
    if "cycles_samples" in snapshot and hasattr(scene, "cycles"):
        scene.cycles.samples = int(snapshot["cycles_samples"])
    if scene.get(ASTRA_RESTORE_KEY) is not None:
        del scene[ASTRA_RESTORE_KEY]
    return failed


def astra_hex_to_linear(value):
    """'#rrggbb' (sRGB, as people write colours) to a linear (r, g, b) tuple."""
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError("Colours are '#rrggbb', got " + repr(value))
    channels = []
    for index in range(3):
        srgb = int(text[index * 2 : index * 2 + 2], 16) / 255.0
        if srgb <= 0.04045:
            channels.append(srgb / 12.92)
        else:
            channels.append(((srgb + 0.055) / 1.055) ** 2.4)
    return tuple(channels)


def astra_linear_to_hex(color):
    """A linear (r, g, b[, a]) colour to '#rrggbb' sRGB, clamped to 0..1."""
    out = "#"
    for index in range(3):
        linear = max(0.0, min(1.0, float(color[index])))
        if linear <= 0.0031308:
            srgb = linear * 12.92
        else:
            srgb = 1.055 * math.pow(linear, 1.0 / 2.4) - 0.055
        out += "%02x" % int(round(max(0.0, min(1.0, srgb)) * 255))
    return out
