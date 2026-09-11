"""Schemas for bounded assembly and animation helpers."""

from mcp.types import Tool

NAME = {"type": "string", "minLength": 1, "maxLength": 128}
NAMES = {"type": "array", "items": NAME, "minItems": 1, "maxItems": 100, "uniqueItems": True}
VECTOR = {
    "type": "array",
    "items": {"type": "number", "minimum": -100000, "maximum": 100000},
    "minItems": 3,
    "maxItems": 3,
}


def tool(name, description, properties, required=()):
    return Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    )


def tools():
    return [
        tool(
            "astra_assemble_parts",
            "Parent distinct parts to an Empty assembly root without moving them. "
            "Choose a main-body anchor; attached pieces get explicit gap checks. Assemble before animation/constraints. "
            "Include every glass panel, wheel and trim piece that must follow the root.",
            {
                "root_name": NAME,
                "names": NAMES,
                "anchor": NAME,
                "max_gap": {"type": "number", "minimum": 0, "maximum": 10},
            },
            ["root_name", "names", "anchor"],
        ),
        tool(
            "astra_place_on_ground",
            "Translate a whole assembly so its lowest evaluated mesh bound rests on a "
            "separate horizontal ground mesh. Preserves relative part positions. Use before root animation.",
            {
                "root_name": NAME,
                "ground_name": NAME,
                "clearance": {"type": "number", "minimum": 0, "maximum": 10},
            },
            ["root_name", "ground_name"],
        ),
        tool(
            "astra_keyframe_object",
            "Create location/XYZ Euler rotation/scale keys on an unanimated object or "
            "assembly root. Values are LOCAL to its parent; rotations are radians. Use distinct increasing frames. "
            "Each animated channel needs two keys. Children inherit root motion. Existing actions/constraints are "
            "rejected to prevent silent overwrite; edit advanced rigs explicitly with bpy.",
            {
                "name": NAME,
                "fps": {"type": "integer", "minimum": 1, "maximum": 60},
                "interpolation": {"enum": ["LINEAR", "BEZIER", "CONSTANT"]},
                "keys": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 24,
                    "items": {
                        "type": "object",
                        "properties": {
                            "frame": {"type": "integer", "minimum": 1, "maximum": 1440},
                            "location": VECTOR,
                            "rotation": VECTOR,
                            "scale": {
                                **VECTOR,
                                "items": {"type": "number", "minimum": 0.001, "maximum": 1000},
                            },
                        },
                        "required": ["frame"],
                        "minProperties": 2,
                        "additionalProperties": False,
                    },
                },
            },
            ["name", "keys"],
        ),
        tool(
            "astra_inspect_animation",
            "Inspect action channels and evaluated poses at up to five frames "
            "(default start/middle/end). Temporarily changes the playhead and restores it in finally. "
            "Numerical evidence includes camera, ground and attachment checks; does not bake simulations.",
            {
                "frames": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 100000},
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                }
            },
        ),
    ]


def report(data, diagnose):
    return {
        **data,
        "samples": [{"frame": s["frame"], "scene": diagnose(s["scene"])} for s in data["samples"]],
    }
