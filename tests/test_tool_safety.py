"""Every registered Blender tool builds a script upstream safe mode accepts.

Upstream blender-mcp refuses a script it cannot validate (MCP safe mode).
A trusted tool whose script is refused is a broken tool, and before this
test such a refusal was only found live. Each spec is built at its typical
and maximum examples and with hostile strings in every free-text argument,
then checked with the upstream validator itself, plus margins below the
upstream limits (depth 24, 20,000 AST nodes, 200,000 bytes) so a tool does
not sit on the edge.
"""

import ast
import copy
import json

import pytest
from blender_mcp.safe_mode import is_safe
from jsonschema import validate

from astra_blender import registry, scripts
from astra_blender.errors import ToolFailure

MAX_DEPTH = 20
MAX_NODES = 8000
MAX_BYTES = 60_000
HOSTILE = [
    "it's \"quoted\"",
    "back\\slash\\n",
    "__import__('os').system('x')",
    "emoji 🚗 ñandú",
    "'); import os; print('",
    "\"\"\"triple\"\"\"",
]
BLENDER_SPECS = [spec for spec in registry.all_specs() if spec.kind == "blender"]


def depth(code):
    """Nesting depth the way upstream's _check_depth counts it."""
    deepest, stack = 0, [(ast.parse(code), 0)]
    while stack:
        node, level = stack.pop()
        deepest = max(deepest, level)
        stack.extend((child, level + 1) for child in ast.iter_child_nodes(node))
    return deepest


def hostile(schema, value, text, counter=None):
    """value with every free string (no enum, pattern or format) replaced by a
    unique variant of text, cut to the schema's maxLength."""
    counter = counter if counter is not None else [0]
    if isinstance(value, str):
        if any(key in schema for key in ("enum", "pattern", "format", "const")):
            return value
        counter[0] += 1
        suffix = f"#{counter[0]}"
        limit = schema.get("maxLength")
        head = text if limit is None else text[: max(0, limit - len(suffix))]
        return head + suffix
    if isinstance(value, list):
        items = schema.get("items", {})
        return [hostile(items if isinstance(items, dict) else {}, item, text, counter) for item in value]
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        return {key: hostile(properties.get(key, {}), item, text, counter) for key, item in value.items()}
    return value


def variants(spec):
    for label in ("typical", "max"):
        yield label, spec.examples[label]
    if "hostile" in spec.examples:
        yield "hostile", spec.examples["hostile"]
    for index, text in enumerate(HOSTILE):
        yield f"hostile{index}", hostile(spec.schema, copy.deepcopy(spec.examples["max"]), text)


CASES = [(spec, label, args) for spec in BLENDER_SPECS for label, args in variants(spec)]


def check(code):
    ok, reason = is_safe(code)
    assert ok is True, reason
    assert depth(code) <= MAX_DEPTH
    assert sum(1 for _ in ast.walk(ast.parse(code))) <= MAX_NODES
    assert len(code.encode("utf-8")) <= MAX_BYTES


@pytest.mark.parametrize(("spec", "label", "args"), CASES, ids=[f"{s.name}-{label}" for s, label, _ in CASES])
def test_every_tool_script_passes_safe_mode(spec, label, args):
    validate(args, spec.schema)
    injected = {name: None for name in spec.injected}
    code = scripts.build(spec, args, injected)
    check(code)
    # repr() kept every hostile string a literal: the trailer still calls the
    # entry once, with exactly the arguments given.
    call = ast.parse(code).body[-1].value.args[0].right.args[0]
    assert call.func.id == spec.entry
    assert ast.literal_eval(call.keywords[0].value) == {**args, **injected}


@pytest.mark.parametrize("spec", BLENDER_SPECS, ids=[s.name for s in BLENDER_SPECS])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused_before_building(spec, bad):
    args = copy.deepcopy(spec.examples["typical"])
    args["_nested"] = {"deep": [1.0, bad]}
    with pytest.raises(ToolFailure, match="numbers must be finite"):
        scripts.build(spec, args)
    with pytest.raises(ToolFailure, match="numbers must be finite"):
        scripts.build(spec, spec.examples["typical"], {"budget_s": bad})


def test_json_nan_from_a_model_is_what_this_guards_against():
    # json.loads accepts NaN and jsonschema bounds let it through; repr()
    # would then write the bare name nan, which safe mode rejects.
    args = json.loads('{"objects": ["A"], "margin": NaN}')
    spec = registry.get("astra_frame_camera")
    validate(args, spec.schema)
    assert is_safe("x = " + repr(args))[0] is False
    with pytest.raises(ToolFailure):
        scripts.build(spec, args)


def test_the_harness_save_snippet_is_safe():
    for path in ["C:/runs/abc/scene.blend", "C:/runs/it's \"odd\"/scene.blend", "/tmp/ñ 🚗/checkpoint.blend"]:
        code = scripts.save_code(path)
        check(code)
        assert ast.literal_eval(ast.parse(code).body[1].value.keywords[0].value) == path


def test_hostile_injected_values_stay_literals():
    spec = registry.get("astra_frame_camera")
    code = scripts.build(spec, spec.examples["typical"], {"output_dir": "C:/x'); import os; ('"})
    check(code)


def test_common_and_measure_helpers_are_safe_together():
    code = scripts.source("common.py") + "\n" + scripts.source("measure.py") + "\nprint(astra_clock())"
    check(code)

