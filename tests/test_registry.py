"""The tool registry: discovery, the ToolSpec contract, and 0.3.2 compatibility."""

import json
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, validate

from astra_blender import engine, motion, registry, scripts, spatial
from astra_blender.registry import PostResult, Recipe, RegistryError, ToolSpec
from astra_blender.tools import TOOL_MODULES

PORTED = [
    "astra_inspect_scene",
    "astra_frame_camera",
    "astra_assemble_parts",
    "astra_place_on_ground",
    "astra_keyframe_object",
    "astra_inspect_animation",
]
LEGACY_TOOLS = {
    entry["name"]: entry
    for entry in json.loads((Path(__file__).parent / "fixtures" / "trusted_tools_0_3_2.json").read_text("utf-8"))
}


def still_ported():
    """Names a workstream has not upgraded yet (each tools module's PORTED_0_3_2)."""
    return [name for module in registry.modules() for name in getattr(module, "PORTED_0_3_2", ())]


def legacy_tool_code(name, args):
    """spatial.tool_code exactly as 0.3.2 shipped it (frame_camera aside)."""
    folder = scripts.SCRIPTS
    if name == "astra_inspect_scene":
        return (
            (folder / "projection.py").read_text(encoding="utf-8")
            + "\n"
            + (folder / "scene_probe.py").read_text(encoding="utf-8")
            + "\nimport json\nprint('ASTRA_SCENE_JSON:' + json.dumps(astra_scene_probe(False, known={}), allow_nan=False))"
        )
    script, function = {
        "astra_assemble_parts": ("assembly.py", "astra_assemble"),
        "astra_place_on_ground": ("assembly.py", "astra_ground"),
        "astra_keyframe_object": ("animation.py", "astra_keyframes"),
        "astra_inspect_animation": ("animation.py", "astra_animation_report"),
    }[name]
    prefix = ""
    if name in {"astra_inspect_animation", "astra_keyframe_object"}:
        prefix = (
            (folder / "projection.py").read_text(encoding="utf-8")
            + "\n"
            + (folder / "scene_probe.py").read_text(encoding="utf-8")
            + "\n"
        )
    return (
        prefix
        + (folder / script).read_text(encoding="utf-8")
        + "\nimport json\nprint("
        + repr("ASTRA_SCENE_JSON:")
        + " + json.dumps("
        + function
        + "(**"
        + repr(args)
        + "), allow_nan=False))"
    )


@pytest.fixture
def isolated(monkeypatch):
    """Load throwaway tools modules; the real registry comes back afterwards."""
    monkeypatch.setitem(registry._state, "specs", None)
    monkeypatch.setitem(registry._state, "modules", ())
    created = []

    def make(name, tools=(), cards=None, recipes=()):
        module = types.ModuleType(name)
        module.TOOLS, module.PROMPT_CARDS, module.RECIPES = list(tools), dict(cards or {}), list(recipes)
        monkeypatch.setitem(sys.modules, name, module)
        created.append(name)
        return name

    return make


def spec(name, **fields):
    base = dict(
        name=name,
        description=f"Test tool {name}.",
        schema={"type": "object", "properties": {"x": {"type": "number"}}, "additionalProperties": False},
        scripts=("assembly.py",),
        entry="astra_assemble",
        examples={"typical": {"x": 1}, "max": {"x": 2}},
    )
    base.update(fields)
    return ToolSpec(**base)


def test_the_registry_lists_the_six_ported_tools_in_0_3_2_order():
    names = registry.names()
    assert [name for name in names if name in PORTED] == PORTED
    assert {"astra_inspect_scene", "astra_inspect_animation"} <= set(registry.names(readonly=True))
    assert [m.__name__ for m in registry.modules()] == list(TOOL_MODULES)
    assert set(still_ported()) <= set(PORTED)


def test_every_workstream_module_is_pre_seeded():
    # The registration file is complete for 0.4: each workstream only fills
    # its own module, so nobody edits tools/__init__.py.
    assert TOOL_MODULES == (
        "astra_blender.tools.sight",
        "astra_blender.tools.toolkit",
        "astra_blender.tools.motion",
        "astra_blender.tools.judge",
        "astra_blender.tools.guard",
    )
    for module in registry.modules():
        assert isinstance(module.TOOLS, list)
        assert isinstance(module.PROMPT_CARDS, dict)
        assert isinstance(module.RECIPES, list)


@pytest.mark.parametrize("name", PORTED)
def test_offered_definitions_are_identical_to_0_3_2(name):
    if name not in still_ported():
        pytest.skip(f"{name} was deliberately upgraded by its workstream")
    tool = registry.get(name).as_tool()
    assert {"name": tool.name, "description": tool.description, "inputSchema": tool.inputSchema} == LEGACY_TOOLS[name]
    assert name in [t.name for t in spatial.tools()]


@pytest.mark.parametrize("name", [n for n in PORTED if n not in {"astra_inspect_scene", "astra_frame_camera"}])
def test_ported_motion_scripts_are_byte_identical(name):
    if name not in still_ported():
        pytest.skip(f"{name} was deliberately upgraded by its workstream")
    for example in registry.get(name).examples.values():
        assert scripts.build(registry.get(name), example) == legacy_tool_code(name, example)
        assert spatial.tool_code(name, example) == legacy_tool_code(name, example)


def test_the_ported_scene_probe_runs_the_same_code_with_the_same_arguments():
    if "astra_inspect_scene" not in still_ported():
        pytest.skip("astra_inspect_scene was deliberately upgraded by WS1")
    new = scripts.build(registry.get("astra_inspect_scene"), {})
    old = legacy_tool_code("astra_inspect_scene", {})
    body = old.rsplit("\nimport json\n", 1)[0]
    assert new.startswith(body + "\nimport json\n")
    # astra_scene_probe(geometry=False, known=None) treats None as {}: the same call.
    assert new.endswith("json.dumps(astra_scene_probe(**{}), allow_nan=False))")
    assert spatial.probe_code() == old


def test_frame_camera_now_prints_the_marker_and_json():
    if "astra_frame_camera" not in still_ported():
        pytest.skip("astra_frame_camera was deliberately upgraded by WS2")
    code = scripts.build(registry.get("astra_frame_camera"), {"objects": ["A"], "margin": 0.2})
    assert code.endswith(
        "print('ASTRA_SCENE_JSON:' + json.dumps(astra_frame_camera(**{'objects': ['A'], 'margin': 0.2}), "
        "allow_nan=False))"
    )
    assert "def astra_frame_camera(objects, margin=0.12, frames=None)" in code


def test_0_3_2_entry_points_are_shims_over_the_registry():
    legacy = {
        "astra_assemble_parts": ("assembly.py", "astra_assemble"),
        "astra_place_on_ground": ("assembly.py", "astra_ground"),
        "astra_keyframe_object": ("animation.py", "astra_keyframes"),
        "astra_inspect_animation": ("animation.py", "astra_animation_report"),
    }
    for name in still_ported():
        if name in legacy:
            assert spatial.MOTION_FUNCTIONS[name] == legacy[name]
    for name, (script, entry) in spatial.MOTION_FUNCTIONS.items():
        assert registry.get(name).entry == entry and script in registry.get(name).scripts
    motion_tools = [t.name for t in motion.tools()]
    assert [n for n in motion_tools if n in PORTED] == [n for n in PORTED[2:] if n in motion_tools]
    for name in PORTED[2:]:
        assert spatial.tool_code(name, registry.get(name).examples["typical"]) == scripts.build(
            registry.get(name), registry.get(name).examples["typical"]
        )
    with pytest.raises(AttributeError):
        spatial.NOT_A_THING  # noqa: B018


@pytest.mark.parametrize("name", registry.names())
def test_examples_validate_against_the_schema(name):
    found = registry.get(name)
    assert {"typical", "max"} <= set(found.examples)
    assert set(found.examples) <= {"typical", "max", "hostile"}
    for example in found.examples.values():
        validate(example, found.schema)


@pytest.mark.parametrize("found", registry.all_specs(), ids=registry.names())
def test_every_spec_keeps_the_contract(found):
    Draft202012Validator.check_schema(found.schema)
    assert found.schema["type"] == "object"
    assert found.schema.get("additionalProperties") is False
    assert "user_prompt" not in found.schema.get("properties", {})
    if found.schema_small is not None:
        Draft202012Validator.check_schema(found.schema_small)
        assert found.schema_small.get("additionalProperties") is False
        assert "user_prompt" not in found.schema_small.get("properties", {})
    assert not set(found.injected) & set(found.schema.get("properties", {}))
    assert len(found.description.split()) <= registry.DESCRIPTION_WORDS
    assert set(found.stages) <= set(registry.STAGES) and found.stages
    assert set(found.tiers) <= set(registry.TIERS) and found.tiers
    assert registry.structural_problems(found) == []
    assert registry.contract_problems(found) == []


def test_read_only_is_derived_from_the_registry():
    assert engine.READ_ONLY == registry.UPSTREAM_READ_ONLY | set(registry.names(readonly=True))
    # Everything 0.3.2 treated as read-only still is.
    assert {
        "get_scene_info",
        "get_object_info",
        "get_viewport_screenshot",
        "astra_inspect_scene",
        "astra_inspect_animation",
    } <= engine.READ_ONLY


def test_every_recipe_step_validates():
    for recipe in registry.recipes():
        assert recipe.min_tier in registry.TIERS
        for tool, arguments in registry.render_recipe(recipe):
            assert registry.check_args(tool, arguments) == [], (recipe.id, tool)


def test_recipes_check_registered_tools_and_contract_only_tools(isolated):
    good = Recipe(
        id="product_shot",
        keywords=("product", "producto"),
        steps=(
            ("astra_place", {"object": "{subject}", "relation": "on", "target": "{support}"}),
            ("astra_frame_camera", {"objects": ["{subject}"], "margin": "{margin}"}),
        ),
        defaults={"subject": "Mug", "support": "Table", "margin": 0.1},
        min_tier="small",
    )
    bad = Recipe(id="broken", steps=(("astra_place", {"object": "Mug", "relation": "onto", "target": "T"}),))
    name = isolated("fake_toolkit", tools=[registry.get("astra_frame_camera")], recipes=[good, bad])
    registry.load([name])
    rendered = registry.render_recipe(good, {"subject": "Cup"})
    assert rendered[1] == ("astra_frame_camera", {"objects": ["Cup"], "margin": 0.1})
    assert all(registry.check_args(tool, args) == [] for tool, args in rendered)
    problems = [registry.check_args(tool, args) for tool, args in registry.render_recipe(bad)]
    assert problems[0] and "onto" in problems[0][0]
    assert registry.check_args("astra_nothing", {}) == ["astra_nothing is neither registered nor in tools/contract.json"]


def test_duplicate_names_and_broken_specs_fail_at_load(isolated):
    first = isolated("fake_a", tools=[spec("astra_twin")])
    second = isolated("fake_b", tools=[spec("astra_twin")])
    with pytest.raises(RegistryError, match="Duplicate tool name 'astra_twin'"):
        registry.load([first, second])
    for broken, reason in [
        (spec("astra_x1", scripts=("no_such_file.py",)), "missing script"),
        (spec("astra_x2", entry=None), "entry function"),
        (spec("astra_x3", kind="harness"), "handler"),
        (spec("astra_x4", injected=("x",)), "must not be model arguments"),
        (spec("astra_x5", scripts=("measure.py",), common=False), "relies on common.py"),
        (spec("astra_x6", chunk_key="next_frame"), "start_from"),
        (spec("astra_x7", injected=("pattern",)), "inject"),
        (spec("astra_x8", stages=("build",)), "stages"),
        (spec("astra_x9", readonly=True, destructive=True), "both readonly and destructive"),
    ]:
        with pytest.raises(RegistryError, match=reason):
            registry.load([isolated("fake_" + broken.name, tools=[broken])])


def test_offered_menus_respect_stage_tier_vision_hidden_and_priority(isolated):
    tools = [
        spec("astra_late", stages=("layout",), priority=200),
        spec("astra_early", stages=("layout",), priority=10),
        spec("astra_big_only", stages=("layout",), tiers=("medium", "large")),
        spec("astra_blind_only", stages=("layout",), vision=False),
        spec("astra_alias", stages=("layout",), hidden=True),
        spec("astra_review", stages=("review",), readonly=True),
    ]
    registry.load([isolated("fake_menu", tools=tools)])
    names = [s.name for s in registry.offered("layout", "large", vision=False)]
    assert names == ["astra_early", "astra_big_only", "astra_blind_only", "astra_late"]
    assert [s.name for s in registry.offered("layout", "small", vision=True)] == ["astra_early", "astra_late"]
    assert [s.name for s in registry.offered("layout", "large", vision=False, limit=2)] == [
        "astra_early",
        "astra_big_only",
    ]
    assert "astra_alias" in registry.names() and "astra_alias" in registry.hidden_names()
    with pytest.raises(ValueError):
        registry.offered("build", "large")


def test_schema_small_is_offered_to_the_small_tier(isolated):
    small = {"type": "object", "properties": {}, "additionalProperties": False}
    registry.load([isolated("fake_small", tools=[spec("astra_sized", schema_small=small)])])
    assert registry.schema_for("astra_sized", "small") == small
    assert "x" in registry.schema_for("astra_sized", "large")["properties"]
    assert registry.get("astra_sized").as_tool("small").inputSchema == small


def test_cards_follow_offered_modules_and_never_grow_past_the_tier(isolated):
    one = isolated(
        "fake_cards_one", tools=[spec("astra_one")], cards={"large": "ONE large", "small": "ONE small"}
    )
    two = isolated("fake_cards_two", tools=[spec("astra_two")], cards={"large": "TWO large"})
    registry.load([one, two])
    assert registry.cards("large") == "ONE large\n\nTWO large"
    assert registry.cards("medium") == "ONE small"
    assert registry.cards("large", ["astra_two"]) == "TWO large"
    assert registry.cards("small", ["astra_two"]) == ""


def test_contract_lists_every_planned_tool_with_valid_argument_schemas():
    contract = registry.contract()
    planned = contract["tools"]
    for name in [
        "astra_scene_facts", "astra_look_check", "astra_motion_facts", "astra_plan_view",
        "astra_build_object", "astra_material", "astra_arrange", "astra_transform", "astra_place",
        "astra_repair_mesh", "astra_cleanup", "astra_light_rig", "astra_camera_shot", "astra_render_setup",
        "astra_set_exposure", "astra_render", "astra_encode_video", "astra_animate", "astra_explode",
        "astra_split_parts", "astra_set_spec", "astra_check", "astra_api_lookup", *PORTED,
    ]:  # fmt: skip
        assert name in planned, name
    for name, entry in planned.items():
        assert entry["module"] in TOOL_MODULES, name
        assert entry["kind"] in registry.KINDS
        Draft202012Validator.check_schema(entry["args"])
        assert entry["args"]["type"] == "object"
        for key in entry.get("injected", ()):
            assert key not in entry["args"]["properties"], (name, key)
    assert planned["astra_cleanup"]["destructive"] is True
    assert planned["astra_render"]["chunk_key"] == "next_frame"
    for event in ["evidence", "spec", "qa", "look", "motion", "render", "artifact", "guard", "autofix",
                  "regression", "approval", "phase", "started", "connected"]:  # fmt: skip
        assert event in contract["events"], event
    assert contract["tier_names"] == list(registry.TIERS)
    assert contract["stage_names"] == list(registry.STAGES)


def test_contract_problems_catch_a_broken_promise():
    ground = registry.get("astra_place_on_ground")
    schema = json.loads(json.dumps(ground.schema))
    schema["properties"]["floor_name"] = schema["properties"].pop("ground_name")
    schema["required"] = ["root_name", "floor_name"]
    problems = registry.contract_problems(replace(ground, schema=schema))
    assert any("ground_name: pinned argument is missing" in p for p in problems)
    assert any("floor_name" in p for p in problems)
    keyframe = registry.get("astra_keyframe_object")
    schema = json.loads(json.dumps(keyframe.schema))
    schema["properties"]["interpolation"]["enum"] = ["LINEAR"]
    assert any("BEZIER" in p for p in registry.contract_problems(replace(keyframe, schema=schema)))
    assert registry.contract_problems(replace(keyframe, readonly=True))


def test_markdown_lists_every_tool():
    table = registry.markdown()
    for name in PORTED:
        assert f"| {name} |" in table


def test_post_result_defaults_are_empty():
    result = PostResult(text="ok")
    assert (result.files, result.events, result.snapshot) == ({}, (), None)
