"""Build the trusted Blender scripts, and read what Blender sends back.

Every registry tool of kind 'blender' runs the same way: its script files are
concatenated with a one-line trailer that calls the entry function with the
schema-validated arguments and prints ASTRA_SCENE_JSON: + JSON. The script
is sent through upstream execute_blender_code, so it must pass upstream safe
mode (tests/test_tool_safety.py proves it for every registered tool).
"""

import json
import math
import re
from pathlib import Path

from .errors import TOOL_ERROR, ToolFailure

SCRIPTS = Path(__file__).parent / "blender_scripts"
MARKER = "ASTRA_SCENE_JSON:"
# Helpers every new tool gets for free, and the calibrated measurement
# helpers a tool opts into by listing measure.py (docs/tool-contract.md).
COMMON = "common.py"
MEASURE = "measure.py"
# The engine keeps model-facing tool results under this many characters.
RESULT_LIMIT = 20000

_ERROR_START = re.compile(r"(?i)^\s*(error\b|failed\b)")
_SAFE_MODE = re.compile(r"^\s*(?:TOOL ERROR: )?Rejected by safe mode")
# WS5's guard wraps raw scripts so an exception prints this marker, with the
# line, on a line of its own instead of failing without partial output. It
# starts a line, or follows upstream's "Code executed successfully: " prefix
# when it is the first line printed. Anchored so an object named
# "Label: ASTRA_SCRIPT_ERROR line 2" inside printed JSON is not an error.
_SCRIPT_ERROR = re.compile(r"(?m)^(?:Code executed successfully: )?ASTRA_SCRIPT_ERROR line \d+")

NOT_FINITE = "TOOL ERROR: numbers must be finite"
NO_MARKER = (
    "TOOL ERROR: Blender did not return scene data; inspect the MCP error or safe-mode settings."
)


def source(name):
    """The text of one file in blender_scripts/."""
    path = SCRIPTS / name
    if path.parent != SCRIPTS:
        raise ValueError(f"Script names are plain file names in blender_scripts/: {name!r}")
    return path.read_text(encoding="utf-8")


def check_finite(value, where="arguments"):
    """Refuse NaN and infinities anywhere in a JSON-like value.

    json.loads accepts NaN and Infinity and jsonschema's bounds let them
    through, but repr() writes them as the bare names nan and inf, which safe
    mode rejects as unknown names - after the approval was already spent.
    """
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolFailure(f"{NOT_FINITE} ({where} holds {value!r})")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            check_finite(item, f"{where}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            check_finite(item, f"{where}[{index}]")


def script_files(spec):
    """The ordered, de-duplicated files a spec's script is built from."""
    names = [name for name in dict.fromkeys(spec.scripts) if name != COMMON]
    if MEASURE in names:
        names.remove(MEASURE)
        names.insert(0, MEASURE)
    return ([COMMON] if spec.common else []) + names


def trailer(entry, arguments):
    """The single line pair that runs the entry and prints the marker line."""
    return (
        "import json\nprint("
        + repr(MARKER)
        + " + json.dumps("
        + entry
        + "(**"
        + repr(arguments)
        + "), allow_nan=False))"
    )


def build(spec, args, injected=None):
    """The complete script for one call of a blender-kind spec.

    `args` are the model's schema-validated arguments; `injected` the
    harness-only ones (output_dir, save_to, start_from, budget_s, known...),
    which win on a name clash. Both are written with repr(), so any string a
    model sends - quotes, backslashes, "__import__('os')" - stays a literal.
    """
    if spec.kind != "blender" or not spec.entry:
        raise ValueError(f"{spec.name} is not a Blender tool")
    arguments = {**(args or {}), **(injected or {})}
    check_finite(arguments)
    body = "\n".join(source(name) for name in script_files(spec))
    return body + "\n" + trailer(spec.entry, arguments)


def result_texts(result):
    """The text blocks of an MCP CallToolResult, or a plain string."""
    if isinstance(result, str):
        return [result]
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    if getattr(result, "structuredContent", None):
        texts.append(json.dumps(result.structuredContent))
    return texts


def classify(text, is_error=False):
    """(text, is_error) for one tool result text.

    Flags the upstream error shapes ("Error executing code: ...", "failed"),
    a safe-mode rejection - which upstream returns as a plain success string
    and 0.3.2 let through unflagged - and the guard's ASTRA_SCRIPT_ERROR.
    A flagged text is prefixed "TOOL ERROR: " once.
    """
    flagged = bool(
        is_error
        or _ERROR_START.match(text)
        or _SAFE_MODE.match(text)
        or _SCRIPT_ERROR.search(text)
        or text.startswith(TOOL_ERROR)
    )
    if flagged and not text.startswith(TOOL_ERROR):
        text = TOOL_ERROR + text
    return text, flagged


def rejected_by_safe_mode(text):
    """True when upstream refused the script, so nothing ran in Blender."""
    return bool(_SAFE_MODE.match(text))


def parse(result):
    """The dict a trusted script printed after the marker.

    Takes the first ASTRA_SCENE_JSON line. A version field, when present
    (`v` for 0.4 payloads, `schema_version` for 0.3.2 ones), must be 1.
    Raises ToolFailure with model-visible text on an error result, a safe-
    mode rejection, a missing marker or unreadable JSON.
    """
    texts = result_texts(result)
    is_error = bool(getattr(result, "isError", False))
    for text in texts:
        flagged_text, flagged = classify(text, is_error)
        if flagged:
            raise ToolFailure(flagged_text[:RESULT_LIMIT])
    for text in texts:
        for line in text.splitlines():
            if MARKER not in line:
                continue
            payload = line.split(MARKER, 1)[1]
            try:
                data, _ = json.JSONDecoder().raw_decode(payload)
            except json.JSONDecodeError as error:
                raise ToolFailure(
                    f"TOOL ERROR: Blender returned unreadable data after the result marker: {error.msg}"
                ) from None
            if not isinstance(data, dict):
                raise ToolFailure("TOOL ERROR: Blender returned a result that is not a JSON object.")
            for key in ("v", "schema_version"):
                if key in data and data[key] != 1:
                    raise ToolFailure(f"TOOL ERROR: Unsupported result version {key}={data[key]!r}.")
            return data
    raise ToolFailure(NO_MARKER)


def cap(text, limit=RESULT_LIMIT):
    """Keep the head and the tail of a long text, and say what was cut.

    The tail is where errors and final summaries land, so it is never the
    part that disappears.
    """
    if len(text) <= limit:
        return text
    note = "\n[... {} characters omitted ...]\n"
    room = max(0, limit - len(note.format(len(text))))
    head = room * 2 // 3
    tail = room - head
    omitted = len(text) - head - tail
    return text[:head] + note.format(omitted) + (text[-tail:] if tail else "")


def save_code(path):
    """The harness's own scene save: a copy, so the user's file is untouched."""
    return "import bpy\nbpy.ops.wm.save_as_mainfile(filepath=" + repr(path) + ", copy=True)\nprint('Saved scene copy')"
