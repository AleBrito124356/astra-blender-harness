"""WS2 Toolkit: make and shoot - modelling, materials, layout, light, camera, render.

Foundation state: the 0.3.2 astra_frame_camera, ported with its schema and
script. One change: it now prints the ASTRA_SCENE_JSON marker plus JSON like
every other trusted tool, instead of a Python dict repr. WS2 adds the
toolkit tools here and turns astra_frame_camera into a hidden alias of
astra_camera_shot.
"""

from ..registry import PostResult, ToolSpec  # noqa: F401 - PostResult is part of this module's API

LONG_NAME = "N" * 124


def frame_camera_summary(args):
    names = args.get("objects", [])
    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    frames = args.get("frames")
    across = f" across frames {', '.join(map(str, frames))}" if frames else ""
    return f"Move the active camera to fit {shown}{across}; only the camera changes."


# Specs still exactly as 0.3.2 shipped them. tests/test_registry.py pins their
# definitions and generated code while they are listed here; remove a name when
# this workstream deliberately upgrades that tool.
PORTED_0_3_2 = ('astra_frame_camera',)

TOOLS = [
    ToolSpec(
        name="astra_frame_camera",
        description="Fit the active Blender camera around exact subject object "
        "names from astra_inspect_scene. Exclude huge floors/backgrounds. Modifies only the camera; preserves "
        "its viewing direction. For animation pass up to five frames to fit combined motion bounds; restores "
        "playhead. Inspect again before rendering.",
        schema={
            "type": "object",
            "properties": {
                "objects": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
                "margin": {"type": "number", "minimum": 0.02, "maximum": 0.35},
                "frames": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {"type": "integer", "minimum": 1, "maximum": 100000},
                },
            },
            "required": ["objects"],
            "additionalProperties": False,
        },
        scripts=("projection.py", "frame_camera.py"),
        entry="astra_frame_camera",
        common=False,
        stages=("layout", "look", "motion", "refine", "finalize"),
        summarize=frame_camera_summary,
        examples={
            "typical": {"objects": ["Car", "Wheel.FL"], "margin": 0.12, "frames": [1, 60, 120]},
            "max": {
                "objects": [LONG_NAME + str(i).zfill(3) for i in range(100)],
                "margin": 0.35,
                "frames": [1, 25000, 50000, 75000, 100000],
            },
        },
    ),
]

PROMPT_CARDS = {}

RECIPES = []
