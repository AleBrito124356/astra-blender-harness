"""The pinned interfaces WS6 codes against: signatures and placeholder behaviour.

Owners replace the bodies (WS1 perception, WS4 qa and spec, WS5 guard and
actions); the signature tests stay and must keep passing.
"""

import inspect

import pytest

from astra_blender import actions, guard, perception, qa, spec
from astra_blender.errors import ActionError, GuardRejection, ToolFailure, TruncatedReply
from astra_blender.provider import parse_json_action


@pytest.mark.parametrize(
    ("function", "signature"),
    [
        (perception.card, "(facts, *, tier, checks=None, motion=None, look=None)"),
        (perception.diff, "(prev, cur, *, checks_prev=None, checks_cur=None)"),
        (perception.stub, "(k, summary)"),
        (perception.subjects, "(facts, roles=None)"),
        (qa.evaluate, "(facts, spec=None, motion=None, look=None, previous=None)"),
        (qa.checklist_text, "(report, tier, limit=12)"),
        (qa.report_markdown, "(report, spec=None, manifest=None)"),
        (spec.draft, "(brief)"),
        (spec.validate, "(spec)"),
        (spec.normalize, "(spec)"),
        (guard.prepare, "(code, tier='large')"),
        (guard.interpret, "(text, prepared)"),
        (actions.parse, "(content, allowed=None, max_actions=8)"),
    ],
)
def test_signatures_are_pinned(function, signature):
    assert str(inspect.signature(function)) == signature


FACTS = {
    "floor": {"name": "Ground", "top_z": 0.0},
    "objects": [
        {"name": "Ground", "root": "Ground", "dims": [20, 20, 0]},
        {"name": "CarBody", "root": "Car", "role": "subject", "dims": [4.2, 1.8, 1.4]},
        {"name": "Wheel.FL", "root": "Car", "role": "part", "dims": [0.7, 0.25, 0.7]},
        {"name": "Mug", "root": "Mug", "role": "prop", "dims": [0.1, 0.1, 0.12]},
    ],
}


def test_perception_placeholders_work():
    card = perception.card(FACTS, tier="small")
    assert card.startswith("SCENE FACTS\n") and "CarBody" in card
    long_facts = {"objects": [{"name": f"Part{i}", "dims": [1, 2, 3]} for i in range(500)]}
    assert len(perception.card(long_facts, tier="small")) < 2600
    with pytest.raises(ValueError):
        perception.card(FACTS, tier="huge")
    moved = {**FACTS, "objects": FACTS["objects"][:3] + [{"name": "Lamp", "root": "Lamp"}]}
    moved["objects"][1] = {**moved["objects"][1], "dims": [4.5, 1.8, 1.4]}
    changes = perception.diff(FACTS, moved, checks_prev=[{"id": "floating:Mug"}], checks_cur=["exposure"])
    assert "added: Lamp" in changes and "removed: Mug" in changes and "changed: CarBody" in changes
    assert "resolved checks: floating:Mug" in changes and "new failing checks: exposure" in changes
    assert perception.diff(FACTS, FACTS) == "CHANGES\nno measured change"
    assert perception.stub(3, "facts card, 1.9k tokens") == "[evidence #3 superseded: facts card, 1.9k tokens]"
    assert perception.subjects(FACTS) == ["Car", "Mug"]
    assert perception.subjects(FACTS, roles=["subject"]) == ["Car"]


def test_qa_placeholders_compare_with_the_previous_report():
    previous = qa.QAReport(
        score=60,
        checks=[
            {"id": "floating:Mug", "level": "error", "passed": False, "fix": {"tool": "astra_place", "arguments": {}}},
            {"id": "exposure", "level": "warning", "passed": True},
        ],
    )
    report = qa.evaluate({"objects": []}, previous=previous)
    assert report.score is None and report.checks == []
    assert report.resolved == ["floating:Mug"] and report.new == [] and report.regressed == []
    assert report.to_dict()["total"] == 0
    assert "floating:Mug" in qa.checklist_text(previous, "large") and "-> astra_place" in qa.checklist_text(previous, "large")
    assert qa.checklist_text(report, "small") == "QA unmeasured: no failing checks."
    assert qa.report_markdown(previous, {"title": "Red car"}, {"files": ["scene.blend"]}).startswith("# Red car")


def test_spec_placeholders_validate_the_shape_ws4_relies_on():
    draft = spec.draft("  Un coche rojo   sobre una mesa ")
    assert draft == {"schema": "astra.spec/1", "title": "Un coche rojo sobre una mesa", "objects": []}
    assert spec.validate(draft) == []
    broken = {"title": "", "objects": [{"id": "car", "names": ["Car*"], "role": "hero"}, {"id": "car"}]}
    paths = [error["path"] for error in spec.validate(broken)]
    assert paths == ["title", "objects[0].role", "objects[1].id", "objects[1].names", "objects[1].role"]
    assert spec.validate([]) == [{"path": "", "message": "a spec is a JSON object"}]
    assert spec.normalize({"title": "x", "objects": [{"id": "a"}]})["objects"][0]["count"] == [1, 1]


def test_guard_placeholder_passes_scripts_through():
    prepared = guard.prepare("import bpy\nprint(1)", "small")
    assert prepared.send_code == prepared.display_code == "import bpy\nprint(1)"
    assert guard.interpret("Code executed successfully: 1", prepared) == ("Code executed successfully: 1", False)
    text, failed = guard.interpret("Error executing code: boom", prepared)
    assert failed and text == "TOOL ERROR: Error executing code: boom"
    assert guard.interpret(text, prepared) == (text, True)
    assert issubclass(GuardRejection, ToolFailure)


@pytest.mark.parametrize(
    "content",
    ['{"tool":"astra_check","arguments":{"limit":3}}', '```json\n{"done":"ok"}\n```', "[]", "{}", "prose"],
)
def test_actions_placeholder_matches_parse_json_action(content):
    try:
        expected = parse_json_action(content)
    except (ValueError, TypeError) as error:
        with pytest.raises(ActionError) as caught:
            actions.parse(content, allowed={"astra_check"})
        assert caught.value.text == str(error)
        assert isinstance(caught.value, ValueError)
        return
    [parsed] = actions.parse(content)
    if expected[0] is None:
        assert parsed.is_done and parsed.done == expected[1]
    else:
        assert (parsed.tool, parsed.arguments) == expected and not parsed.is_done


def test_shared_errors_carry_model_visible_text():
    assert ToolFailure("numbers must be finite").text == "TOOL ERROR: numbers must be finite"
    assert ToolFailure("TOOL ERROR: once").text == "TOOL ERROR: once"
    truncated = TruncatedReply(4096)
    assert isinstance(truncated, RuntimeError) and truncated.chars == 4096 and "4096" in str(truncated)
