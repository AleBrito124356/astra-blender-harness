"""Camera projection from Blender's own render frame, compatible with MCP safe mode."""

from mathutils import Vector


def astra_project(scene, camera, point):
    local = camera.matrix_world.normalized().inverted() @ point
    depth = -local.z
    frame = camera.data.view_frame(scene=scene)
    if camera.data.type == "PERSP":
        if abs(depth) < 1e-8:
            return Vector((1e8, 1e8, depth))
        x, y = local.x / depth, local.y / depth
        horizontal = [v.x / -v.z for v in frame]
        vertical = [v.y / -v.z for v in frame]
    else:
        x, y = local.x, local.y
        horizontal = [v.x for v in frame]
        vertical = [v.y for v in frame]
    left, right = min(horizontal), max(horizontal)
    bottom, top = min(vertical), max(vertical)
    return Vector(((x - left) / (right - left), (y - bottom) / (top - bottom), depth))
