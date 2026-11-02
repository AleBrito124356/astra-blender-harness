"""Blender Quality Lab, when installed: its gates and fixes on the live scene.

The lab (github.com/AleBrito124356/blender-quality-lab) ships an inspector that
runs inside a running Blender - ray-cast render visibility, framing and
occlusion, floating groups, light reach, materials, animation keys - and turns
the facts into technical gates and a prioritized "Fix first" list with Blender
Python. Astra runs that inspector as one of its reviewed scripts: the code is
the lab's, never the model's. It uses exec and numpy, so upstream Blender MCP's
safe mode refuses it; the socket and headless backends run it.
"""

from mcp.types import Tool

DESCRIPTION = (
    "Blender Quality Lab's technical gates on the live scene - real render visibility with ray "
    "casts, framing and occlusion, floating groups, light reach, materials, animation keys - and a "
    "prioritized 'Fix first' list with ready-to-run Python. Read-only. Passing gates does not prove "
    "a beautiful image."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 50,
            "description": "Object names or globs that are the subject (default: inferred).",
        },
        "subject_collection": {"type": "string", "maxLength": 128},
        "strict_contact": {
            "type": "boolean",
            "description": "Fail when touching groups float above a surface.",
        },
    },
    "additionalProperties": False,
}

SAFE_MODE_HINT = (
    " Upstream Blender MCP's safe mode refuses the inspector (it uses exec and numpy); use the "
    "socket or headless backend for quality checks."
)


def lab():
    """The lab's integration API when installed and new enough, else None."""
    try:
        from blender_quality import api
    except ImportError:
        return None
    if getattr(api, "API_VERSION", 0) < 1 or not hasattr(api, "live_inspection_code"):
        return None
    return api


def tool(name="astra_quality_check"):
    return Tool(name=name, description=DESCRIPTION, inputSchema=SCHEMA)


def code(api, arguments):
    return api.live_inspection_code(
        subject=arguments.get("subject") or None,
        subject_collection=arguments.get("subject_collection") or None,
    )


def summary(api, output, strict_contact=False):
    """The lab's verdict on a live inspection: (text, inspection, technical report)."""
    inspection = api.parse_live_output(output)
    report = api.score(inspection, strict_contact=strict_contact)
    markdown = api.describe(inspection, fmt="md", strict_contact=strict_contact)
    checks = report.get("checks", [])
    failed = [check.get("id", "?") for check in checks if not check.get("passed", True)]
    passed = report.get("passed", len(checks) - len(failed))
    head = f"Blender Quality Lab: {passed}/{report.get('total', len(checks))} gates pass"
    head += (" - failing: " + ", ".join(failed) if failed else "") + "."
    return head + "\n\n" + markdown, inspection, report
