"""WS3 Motion and Explode: presets, exploded views, part separation, keyframes.

Foundation state: the four 0.3.2 motion tools ported with their schemas and
scripts, byte-identical code included. astra_inspect_animation keeps its
post hook: motion.report folds the sweep, animation.json is written and the
animation event emitted, as 0.3.2 did. WS3 upgrades these specs and adds
astra_animate, astra_explode and astra_split_parts here.
"""

import json

from .. import motion, spatial
from ..motion import NAME, NAMES, VECTOR
from ..registry import PostResult, ToolSpec

LONG_NAME = "N" * 125


def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def inspect_animation_post(data, ctx):
    report = motion.report(data, spatial.diagnostics)
    text = json.dumps(report, ensure_ascii=False)
    return PostResult(
        text=text,
        files={"animation.json": text},
        events=(("animation", {"message": "Evaluated animation poses inspected", "actions": report["actions"]}),),
        snapshot=report,
    )


def assemble_summary(args):
    names = args.get("names", [])
    return (
        f"Parent {len(names)} parts under the Empty {args.get('root_name')!r} anchored on "
        f"{args.get('anchor')!r}, keeping every world position."
    )


def ground_summary(args):
    return f"Move the assembly {args.get('root_name')!r} as a unit so it rests on {args.get('ground_name')!r}."


def keyframe_summary(args):
    keys = args.get("keys", [])
    frames = [key.get("frame") for key in keys]
    span = f"frames {frames[0]}-{frames[-1]}" if frames else "no frames"
    return f"Key {args.get('name')!r} at {len(keys)} frames ({span}); sets the scene frame range and fps."


MAX_KEY = {"location": [100000, -100000, 0], "rotation": [6.283185, -6.283185, 0], "scale": [1000, 0.001, 1]}

# Specs still exactly as 0.3.2 shipped them. tests/test_registry.py pins their
# definitions and generated code while they are listed here; remove a name when
# this workstream deliberately upgrades that tool.
PORTED_0_3_2 = ('astra_assemble_parts', 'astra_place_on_ground', 'astra_keyframe_object', 'astra_inspect_animation')

TOOLS = [
    ToolSpec(
        name="astra_assemble_parts",
        description="Parent distinct parts to an Empty assembly root without moving them. "
        "Choose a main-body anchor; attached pieces get explicit gap checks. Assemble before animation/constraints. "
        "Include every glass panel, wheel and trim piece that must follow the root.",
        schema=_schema(
            {
                "root_name": NAME,
                "names": NAMES,
                "anchor": NAME,
                "max_gap": {"type": "number", "minimum": 0, "maximum": 10},
            },
            ["root_name", "names", "anchor"],
        ),
        scripts=("assembly.py",),
        entry="astra_assemble",
        common=False,
        stages=("layout", "motion", "refine"),
        summarize=assemble_summary,
        examples={
            "typical": {"root_name": "CarRoot", "names": ["CarBody", "Wheel.FL"], "anchor": "CarBody", "max_gap": 0.15},
            "max": {
                "root_name": LONG_NAME + "000",
                "names": [LONG_NAME + str(i).zfill(3) for i in range(100)],
                "anchor": LONG_NAME + "000",
                "max_gap": 10,
            },
        },
    ),
    ToolSpec(
        name="astra_place_on_ground",
        description="Translate a whole assembly so its lowest evaluated mesh bound rests on a "
        "separate horizontal ground mesh. Preserves relative part positions. Use before root animation.",
        schema=_schema(
            {
                "root_name": NAME,
                "ground_name": NAME,
                "clearance": {"type": "number", "minimum": 0, "maximum": 10},
            },
            ["root_name", "ground_name"],
        ),
        scripts=("assembly.py",),
        entry="astra_ground",
        common=False,
        stages=("layout", "motion", "refine"),
        summarize=ground_summary,
        examples={
            "typical": {"root_name": "CarRoot", "ground_name": "Ground", "clearance": 0.0},
            "max": {"root_name": LONG_NAME + "001", "ground_name": LONG_NAME + "002", "clearance": 10},
        },
    ),
    ToolSpec(
        name="astra_keyframe_object",
        description="Create location/XYZ Euler rotation/scale keys on an unanimated object or "
        "assembly root. Values are LOCAL to its parent; rotations are radians. Use distinct increasing frames. "
        "Each animated channel needs two keys. Children inherit root motion. Existing actions/constraints are "
        "rejected to prevent silent overwrite; edit advanced rigs explicitly with bpy.",
        schema=_schema(
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
        scripts=("projection.py", "scene_probe.py", "animation.py"),
        entry="astra_keyframes",
        common=False,
        stages=("motion", "refine"),
        summarize=keyframe_summary,
        examples={
            "typical": {
                "name": "CarRoot",
                "fps": 24,
                "interpolation": "BEZIER",
                "keys": [
                    {"frame": 1, "location": [0, 0, 0]},
                    {"frame": 60, "location": [4, 0, 0], "rotation": [0, 0, 1.5708]},
                    {"frame": 120, "location": [8, 0, 0], "rotation": [0, 0, 3.1416]},
                ],
            },
            "max": {
                "name": LONG_NAME + "003",
                "fps": 60,
                "interpolation": "CONSTANT",
                "keys": [
                    {
                        "frame": 1 + 60 * i,
                        "location": MAX_KEY["location"],
                        "rotation": MAX_KEY["rotation"],
                        "scale": MAX_KEY["scale"],
                    }
                    for i in range(24)
                ],
            },
        },
    ),
    ToolSpec(
        name="astra_inspect_animation",
        description="Inspect action channels and evaluated poses at up to five frames "
        "(default start/middle/end). Temporarily changes the playhead and restores it in finally. "
        "Numerical evidence includes camera, ground and attachment checks; does not bake simulations.",
        schema=_schema(
            {
                "frames": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 100000},
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                }
            }
        ),
        scripts=("projection.py", "scene_probe.py", "animation.py"),
        entry="astra_animation_report",
        common=False,
        readonly=True,
        stages=("motion", "review", "refine", "finalize"),
        post=inspect_animation_post,
        examples={"typical": {"frames": [1, 60, 120]}, "max": {"frames": [1, 25000, 50000, 75000, 100000]}},
    ),
]

PROMPT_CARDS = {}

RECIPES = []
