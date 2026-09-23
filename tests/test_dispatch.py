"""The engine's generic registry dispatch: build, execute, classify, parse, post."""

import asyncio
import json
import re
import sys
import types

import pytest
from conftest import FakeSession, ScriptedProvider, action, run_with
from mcp.types import CallToolResult, TextContent

from astra_blender import engine, registry, scripts
from astra_blender.config import RunConfig
from astra_blender.engine import Run
from astra_blender.errors import ToolFailure
from astra_blender.registry import PostResult, ToolSpec

PLAN = {"role": "assistant", "content": "Plan"}
FRAMED = {"camera": "Camera", "subjects": ["Cube"], "margin": 0.12, "message": "fit"}


def tool_results(run, name):
    return [e for e in run.events if e["type"] == "tool_result" and e["tool"] == name]


def after_edit_audits(provider):
    last = provider.messages[-1]
    return sum("After-edit spatial checks" in str(m.get("content", "")) for m in last)


async def test_a_model_tool_call_runs_the_registry_script_and_reads_marker_json(tmp_path):
    session = FakeSession({"astra_frame_camera": FRAMED})
    provider = ScriptedProvider([PLAN, action("astra_frame_camera", {"objects": ["Cube"], "frames": [1, 24]})])
    run, _ = await run_with(tmp_path, provider=provider, session=session, vision=False)
    assert run.status == "completed"
    [code] = session.codes("astra_frame_camera")
    assert "json.dumps(astra_frame_camera(**{'objects': ['Cube'], 'frames': [1, 24]})" in code
    [result] = tool_results(run, "astra_frame_camera")
    assert not result["is_error"]
    assert "ASTRA_SCENE_JSON:" in result["text"] and '"camera": "Camera"' in result["text"]
    # A mutating trusted tool is followed by the after-edit audit, as in 0.3.2.
    assert after_edit_audits(provider) == 1


async def test_the_inspect_posts_still_write_quality_and_animation_evidence(tmp_path):
    report = {"schema_version": 1, "start": 1, "end": 24, "fps": 24, "actions": [], "samples": []}
    session = FakeSession({"astra_animation_report": report})
    provider = ScriptedProvider([PLAN, action("astra_inspect_animation", {"frames": [1, 12]})])
    run, _ = await run_with(tmp_path, provider=provider, session=session, vision=False, api_key="sk-long-enough-key")
    assert run.status == "completed"
    assert (run.directory / "quality.json").is_file()
    assert json.loads((run.directory / "animation.json").read_text())["actions"] == []
    kinds = [e["type"] for e in run.events]
    assert "quality" in kinds and "animation" in kinds
    # The post's events come before the tool_result that closes the call.
    animation = kinds.index("animation")
    assert kinds[animation + 1] == "tool_result"
    assert run.tool_state["snapshots"]["astra_inspect_scene"]["issues"] is not None


async def test_a_safe_mode_rejection_is_an_error_and_not_a_mutation(tmp_path):
    class Rejecting(FakeSession):
        async def call_tool(self, name, args):
            if name == "execute_blender_code" and "model mutation" in args.get("code", ""):
                self.calls.append((name, args))
                text = "Rejected by safe mode - line 1: import of 'os' is not allowed\n\nBLENDER_MCP_SAFE_MODE..."
                return CallToolResult(content=[TextContent(type="text", text=text)])
            return await super().call_tool(name, args)

    provider = ScriptedProvider([PLAN, action()])
    run, _ = await run_with(tmp_path, provider=provider, session=Rejecting(), vision=False)
    assert run.status == "completed"
    [result] = [r for r in tool_results(run, "execute_blender_code") if "Rejected" in r["text"]]
    assert result["is_error"] and result["text"].startswith("TOOL ERROR: Rejected by safe mode")
    assert after_edit_audits(provider) == 0


async def test_invalid_denied_and_non_finite_calls_are_not_mutations(tmp_path):
    turns = [
        PLAN,
        action("astra_frame_camera", {"objects": []}),
        action("astra_frame_camera", {"objects": ["Cube"], "margin": float("nan")}),
    ]
    provider = ScriptedProvider(turns)
    run, session = await run_with(tmp_path, provider=provider, vision=False)
    assert run.status == "completed"
    results = tool_results(run, "astra_frame_camera")
    assert results == []  # refused before tool_call: nothing was executed
    texts = [m.get("content") for m in provider.messages[-1] if m.get("role") == "tool"]
    assert any("invalid arguments" in t for t in texts)
    assert any("numbers must be finite" in t for t in texts)
    assert session.codes("astra_frame_camera") == []
    assert after_edit_audits(provider) == 0


async def test_a_denied_mutation_changes_nothing(tmp_path):
    class Denying(Run):
        async def approve(self, name, arguments):
            return name in engine.READ_ONLY or "save_as_mainfile" in str(arguments)

    session = FakeSession()
    provider = ScriptedProvider([PLAN, action()])
    run = Denying(RunConfig(prompt="Create a lamp", vision=False), tmp_path)
    from contextlib import asynccontextmanager

    from astra_blender.config import MCPConfig

    @asynccontextmanager
    async def connector(_):
        yield session

    await engine.execute(run, MCPConfig(), provider, connector)
    assert run.status == "completed"
    assert any(e["type"] == "denied" for e in run.events)
    assert after_edit_audits(provider) == 0


async def test_the_approval_card_carries_a_plain_summary(tmp_path):
    run = Run(RunConfig(prompt="Create a lamp"), tmp_path)
    task = asyncio.create_task(run.approve("astra_frame_camera", {"objects": ["Car", "Wheel"], "frames": [1, 48]}))
    await asyncio.sleep(0)
    event = run.events[-1]
    assert event["type"] == "approval"
    assert event["summary"] == "Move the active camera to fit Car, Wheel across frames 1, 48; only the camera changes."
    next(iter(run.approvals.values())).set_result(True)
    assert await task is True
    task = asyncio.create_task(run.approve("execute_blender_code", {"code": "x"}))
    await asyncio.sleep(0)
    assert "summary" not in run.events[-1]
    next(iter(run.approvals.values())).set_result(False)
    await task


@pytest.fixture
def extra_tools(monkeypatch):
    """Register throwaway specs next to the real ones for one test."""

    def install(*specs):
        module = types.ModuleType("fake_extra_tools")
        module.TOOLS, module.PROMPT_CARDS, module.RECIPES = list(specs), {}, []
        monkeypatch.setitem(sys.modules, "fake_extra_tools", module)
        monkeypatch.setitem(registry._state, "specs", None)
        monkeypatch.setitem(registry._state, "modules", ())
        from astra_blender.tools import TOOL_MODULES

        registry.load([*TOOL_MODULES, "fake_extra_tools"])
        monkeypatch.setattr(engine, "READ_ONLY", registry.read_only_names())

    return install


async def test_a_harness_tool_reads_blender_through_ctx_call(tmp_path, extra_tools):
    seen = {}

    async def handler(args, ctx):
        facts = await ctx.call("astra_inspect_scene", {})
        seen["facts"] = facts
        try:
            await ctx.call("astra_frame_camera", {"objects": ["Cube"]})
        except ToolFailure as failure:
            seen["refused"] = failure.text
        ctx.state["checked"] = ctx.state.get("checked", 0) + 1
        return PostResult(
            text=f"checked {args['limit']} of {len(facts.data['objects'])} objects at tier {ctx.tier}",
            files={"qa.json": json.dumps({"key": ctx.config.api_key.get_secret_value()}), "raw.bin": b"\x00\x01"},
            events=(("qa", {"score": 90, "failing": []}),),
            snapshot={"score": 90},
        )

    extra_tools(
        ToolSpec(
            name="astra_check",
            description="Checks the scene.",
            kind="harness",
            handler=handler,
            readonly=True,
            schema={"type": "object", "properties": {"limit": {"type": "integer"}}, "additionalProperties": False},
            examples={"typical": {"limit": 3}, "max": {"limit": 30}},
        )
    )
    provider = ScriptedProvider([action("astra_check", {"limit": 3})])
    run, session = await run_with(tmp_path, provider=provider, vision=False, api_key="sk-long-enough-key")
    assert run.status == "completed"
    assert seen["facts"].data["schema_version"] == 1 and not seen["facts"].is_error
    assert "read-only registry tools" in seen["refused"]
    [result] = tool_results(run, "astra_check")
    assert result["text"] == "checked 3 of 0 objects at tier large"
    # Harness files are redacted like every other run file; bytes are written as given.
    assert "sk-long-enough-key" not in (run.directory / "qa.json").read_text()
    assert (run.directory / "raw.bin").read_bytes() == b"\x00\x01"
    assert any(e["type"] == "qa" and e["score"] == 90 for e in run.events)
    assert run.tool_state["snapshots"]["astra_check"] == {"score": 90}
    assert run.tool_state["checked"] == 1
    # Read-only, so it ran in the plan phase without approval or an after-edit audit.
    nested = [e for e in run.events if e["type"] == "tool_call" and e["tool"] == "astra_inspect_scene"]
    assert any(e["internal"] for e in nested)
    assert after_edit_audits(provider) == 0


async def test_a_chunked_tool_is_looped_inside_one_model_call(tmp_path, extra_tools):
    posted = {}

    def post(data, ctx):
        posted["data"] = data
        return PostResult(text=f"rendered {sum(len(c['files']) for c in data['chunks'])} frames")

    extra_tools(
        ToolSpec(
            name="astra_fake_render",
            description="Renders frames in chunks.",
            schema={"type": "object", "properties": {"frames": {"type": "integer"}}, "additionalProperties": False},
            scripts=("assembly.py",),
            entry="astra_assemble",
            injected=("output_dir", "start_from", "budget_s"),
            chunk_key="next_frame",
            max_seconds=25,
            post=post,
            examples={"typical": {"frames": 3}, "max": {"frames": 3}},
        )
    )

    def chunk(code):
        found = re.search(r"'start_from': (None|\d+)", code).group(1)
        start = 1 if found == "None" else int(found)
        assert "'budget_s': 25.0" in code and "'output_dir': " in code
        return {"files": [f"f{start}.png"], "next_frame": start + 1 if start < 3 else None}

    session = FakeSession({"astra_assemble": chunk})
    provider = ScriptedProvider([PLAN, action("astra_fake_render", {"frames": 3})])
    run, _ = await run_with(tmp_path, provider=provider, session=session, vision=False)
    assert run.status == "completed"
    assert len(session.codes("astra_assemble")) == 3
    assert [c["files"][0] for c in posted["data"]["chunks"]] == ["f1.png", "f2.png", "f3.png"]
    assert posted["data"]["next_frame"] is None
    [result] = tool_results(run, "astra_fake_render")
    assert result["text"] == "rendered 3 frames"
    assert [e for e in run.events if e["type"] == "tool_call" and e["tool"] == "astra_fake_render"][0]


async def test_injected_values_and_hidden_tools(tmp_path, extra_tools):
    def inject(args, ctx):
        return {"pattern": "frame_####"}

    extra_tools(
        ToolSpec(
            name="astra_fake_look",
            description="Looks.",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            scripts=("assembly.py",),
            entry="astra_assemble",
            readonly=True,
            hidden=True,
            injected=("save_to", "pattern", "known"),
            inject=inject,
            examples={"typical": {}, "max": {}},
        )
    )
    session = FakeSession({"astra_assemble": {"v": 1, "ok": True}})
    provider = ScriptedProvider([action("astra_fake_look", {}), action("astra_fake_look", {})])
    run, _ = await run_with(tmp_path, provider=provider, session=session, vision=False)
    codes = session.codes("astra_assemble")
    assert len(codes) == 2
    assert f"'save_to': '{run.directory.as_posix()}/fake_look-001.png'" in codes[0]
    assert "fake_look-002.png" in codes[1]
    assert "'pattern': 'frame_####'" in codes[0] and "'known': {}" in codes[0]
    # Hidden: accepted when called, never offered.
    assert all("astra_fake_look" not in names for names in provider.tools)


async def test_a_post_hook_cannot_write_outside_the_run(tmp_path, extra_tools):
    extra_tools(
        ToolSpec(
            name="astra_escape",
            description="Escapes.",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            kind="harness",
            handler=lambda args, ctx: PostResult(text="x", files={"../evil.txt": "no"}),
            readonly=True,
            examples={"typical": {}, "max": {}},
        )
    )
    provider = ScriptedProvider([action("astra_escape", {})])
    run, _ = await run_with(tmp_path, provider=provider, vision=False)
    [result] = tool_results(run, "astra_escape")
    assert result["is_error"] and "outside the run directory" in result["text"]
    assert not (tmp_path / "evil.txt").exists()


async def test_a_missing_marker_is_a_tool_error_for_the_model(tmp_path):
    class NoMarker(FakeSession):
        async def call_tool(self, name, args):
            if "astra_frame_camera(" in args.get("code", ""):
                self.calls.append((name, args))
                return CallToolResult(content=[TextContent(type="text", text="Code executed successfully: ")])
            return await super().call_tool(name, args)

    provider = ScriptedProvider([PLAN, action("astra_frame_camera", {"objects": ["Cube"]})])
    run, _ = await run_with(tmp_path, provider=provider, session=NoMarker(), vision=False)
    assert run.status == "completed"
    [result] = tool_results(run, "astra_frame_camera")
    assert result["is_error"] and "did not return scene data" in result["text"]


def test_engine_marks_a_safe_mode_rejection_as_an_error():
    rejected = CallToolResult(
        content=[TextContent(type="text", text="Rejected by safe mode - line 1: import of 'os' is not allowed")]
    )
    text, _ = engine.result_parts(rejected)
    assert text.startswith("TOOL ERROR: Rejected by safe mode")
    assert scripts.rejected_by_safe_mode(text.removeprefix("TOOL ERROR: "))
