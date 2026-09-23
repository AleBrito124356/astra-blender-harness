"""WS1 Sight: perception for models with and without vision.

Foundation state: the 0.3.2 audit, astra_inspect_scene, ported unchanged. Its
generated script is the same projection.py + scene_probe.py the live viewer
uses, and its post hook writes quality.json and emits the quality event as
0.3.2 did. WS1 adds astra_scene_facts, astra_look_check, astra_motion_facts
and astra_plan_view here, and turns astra_inspect_scene into a hidden alias.
"""

import json

from .. import spatial
from ..registry import STAGES, PostResult, ToolSpec


def inspect_scene_post(data, ctx):
    report = spatial.diagnostics(data)
    text = json.dumps(report, ensure_ascii=False)
    return PostResult(
        text=text,
        files={"quality.json": text},
        events=(
            (
                "quality",
                {"issues": report["issues"], "message": str(len(report["issues"])) + " scene checks need review"},
            ),
        ),
        snapshot=report,
    )


# Specs still exactly as 0.3.2 shipped them. tests/test_registry.py pins their
# definitions and generated code while they are listed here; remove a name when
# this workstream deliberately upgrades that tool.
PORTED_0_3_2 = ('astra_inspect_scene',)

TOOLS = [
    ToolSpec(
        name="astra_inspect_scene",
        description="Read actual evaluated world bounds, dimensions, camera "
        "projection, material assignments and current Blender API capabilities. Essential for text-only models. "
        "No images and no scene changes.",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        scripts=("projection.py", "scene_probe.py"),
        entry="astra_scene_probe",
        common=False,
        readonly=True,
        stages=STAGES,
        post=inspect_scene_post,
        examples={"typical": {}, "max": {}},
    ),
]

PROMPT_CARDS = {}

RECIPES = []
