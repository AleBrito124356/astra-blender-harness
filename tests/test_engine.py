import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from astra_blender.config import MCPConfig, RunConfig
from astra_blender.demo import DemoSession
from astra_blender.engine import TERMINAL, Run, execute
from astra_blender.provider import parse_json_action


class Session(DemoSession):
    simulated = False

    def __init__(self, error=False):
        self.calls = []
        self.error = error

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.error:
            return CallToolResult(content=[TextContent(type="text", text="Error: Blender unavailable")])
        if name == "execute_blender_code" and "save_as_mainfile" in args["code"]:
            import ast

            call = ast.parse(args["code"]).body[1].value
            path = next(ast.literal_eval(k.value) for k in call.keywords if k.arg == "filepath")
            Path(path).write_bytes(b"BLENDER-test-fixture")
        return await super().call_tool(name, args)


class Provider:
    def __init__(self, responses=None, tokens=10):
        self.responses = iter(responses or [])
        self.messages = []
        self.tokens = tokens

    async def complete(self, messages, tools):
        self.messages.append(json.loads(json.dumps(messages)))
        return next(self.responses, {"role": "assistant", "content": "Phase done"}), {
            "total_tokens": self.tokens
        }


def action(name="execute_blender_code", args=None):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args or {"code": "print('model mutation')"}),
                },
            }
        ],
    }


async def run_with(tmp_path, provider=None, session=None, **config):
    session = session or Session()

    @asynccontextmanager
    async def connector(_):
        yield session

    run = Run(RunConfig(prompt="Create a lamp", auto_approve=True, **config), tmp_path)
    await execute(run, MCPConfig(), provider or Provider(), connector)
    return run, session


async def test_complete_saves_evidence_and_manifest(tmp_path):
    run, _ = await run_with(tmp_path)
    assert run.status == "completed"
    assert (run.directory / "scene.blend").exists()
    assert len(list(run.directory.glob("viewport-*.png"))) == 3
    assert [e["phase"] for e in run.events if e["type"] == "phase"] == [
        "plan",
        "build",
        "review",
        "refine",
        "finalize",
    ]
    assert json.loads((run.directory / "manifest.json").read_text())["status"] == "completed"


async def test_readonly_rejects_model_mutation(tmp_path):
    provider = Provider([action(), {"role": "assistant", "content": "Plan done"}])
    run, session = await run_with(tmp_path, provider=provider)
    assert run.status == "completed"
    assert not any("model mutation" in args.get("code", "") for _, args in session.calls)
    assert any("read-only" in str(msgs) for msgs in provider.messages)


async def test_unknown_and_invalid_tools_not_executed(tmp_path):
    provider = Provider(
        [
            action("not_allowed"),
            action("get_scene_info", {"unexpected": 123}),
            {"role": "assistant", "content": "Done"},
        ]
    )
    run, session = await run_with(tmp_path, provider=provider)
    assert run.status == "completed"
    assert not any(name == "not_allowed" for name, _ in session.calls)


async def test_unfinished_phase_not_success(tmp_path):
    run, _ = await run_with(tmp_path, provider=Provider([action("get_scene_info")] * 5))
    assert run.status == "budget_exhausted"
    assert not (run.directory / "scene.blend").exists()


async def test_token_budget_stops_next_call(tmp_path):
    run, _ = await run_with(tmp_path, provider=Provider(tokens=5000), max_total_tokens=4096)
    assert run.status == "budget_exhausted"
    assert run.steps == 1


async def test_json_repair_and_text_only(tmp_path):
    provider = Provider(
        [{"role": "assistant", "content": "oops"}] + [{"role": "assistant", "content": '{"done":"ok"}'}] * 5
    )
    run, _ = await run_with(tmp_path, provider=provider, tool_mode="json", vision=False)
    assert run.status == "completed"
    assert any(e["type"] == "repair" for e in run.events)
    assert "data:image" not in json.dumps(provider.messages)


async def test_upstream_error_text_fails(tmp_path):
    run, session = await run_with(tmp_path, session=Session(error=True))
    assert run.status == "failed"
    assert len(session.calls) == 1


async def test_save_must_exist(tmp_path):
    class MissingSave(Session):
        async def call_tool(self, name, args):
            return CallToolResult(content=[TextContent(type="text", text="ok")])

    run, _ = await run_with(tmp_path, session=MissingSave())
    assert run.status == "failed"


async def test_key_redacted(tmp_path):
    secret = "test-private-key"
    run, _ = await run_with(
        tmp_path, provider=Provider([{"role": "assistant", "content": secret}]), api_key=secret
    )
    assert secret not in (run.directory / "events.jsonl").read_text()
    assert secret not in (run.directory / "manifest.json").read_text()


async def test_approval_denial_and_cancel(tmp_path):
    run = Run(RunConfig(prompt="Create a lamp"), tmp_path)
    task = asyncio.create_task(run.approve("execute_blender_code", {"code": "x"}))
    await asyncio.sleep(0)
    assert run.status == "awaiting_approval"
    next(iter(run.approvals.values())).set_result(False)
    assert await task is False
    assert not run.approvals
    task = asyncio.create_task(run.approve("execute_blender_code", {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not run.approvals


async def test_provider_failure_does_not_leak(tmp_path):
    class Broken:
        async def complete(self, *args):
            raise RuntimeError("SECRET request body")

    run, _ = await run_with(tmp_path, provider=Broken())
    assert run.status == "failed"
    assert "SECRET" not in (run.directory / "events.jsonl").read_text()


@pytest.mark.parametrize("content", ["[]", '{"tool":3,"arguments":{}}', '{"done":true}', "{}"])
def test_invalid_json_contract(content):
    with pytest.raises((ValueError, TypeError)):
        parse_json_action(content)


def test_fenced_json():
    assert parse_json_action('```json\n{"tool":"get_scene_info","arguments":{}}\n```') == (
        "get_scene_info",
        {},
    )


async def test_a_slow_approval_does_not_spend_the_run_deadline(tmp_path):
    # A run once died with TimeoutError after sitting 16 minutes on an unread
    # approval: human review time was being charged to the model's wall clock.
    session = Session()

    @asynccontextmanager
    async def connector(_):
        yield session

    run = Run(RunConfig(prompt="Create a lamp", timeout_seconds=30), tmp_path)

    async def answer(slow_once=[True]):
        while run.deadline is None:
            await asyncio.sleep(0)
        # A full run needs about two seconds of real work here. Leave four,
        # then take five to answer the first approval: without the pause the
        # deadline fires mid-review, which is exactly the reported failure.
        run.deadline.reschedule(asyncio.get_running_loop().time() + 4)
        while run.status not in TERMINAL:
            for future in list(run.approvals.values()):
                if future.done():
                    continue
                if slow_once[0]:
                    await asyncio.sleep(5)
                    slow_once[0] = False
                future.set_result(True)
            await asyncio.sleep(0.01)

    helper = asyncio.create_task(answer())
    await execute(run, MCPConfig(), Provider(), connector)
    helper.cancel()

    assert run.status == "completed"
    assert run.waited_for_approval >= 5
    assert any(event["type"] == "deadline" for event in run.events)
    manifest = json.loads((run.directory / "manifest.json").read_text())
    # The wait is reported rather than buried inside elapsed_seconds.
    assert manifest["awaiting_approval_seconds"] >= 5


@asynccontextmanager
async def task_group_connector(_, session=None):
    """Repackage errors the way the real MCP client's anyio task groups do."""
    try:
        yield session or Session()
    except BaseException as error:
        # BaseExceptionGroup, as anyio uses: a plain ExceptionGroup cannot hold
        # CancelledError, which is a BaseException.
        raise BaseExceptionGroup(
            "unhandled errors in a TaskGroup",
            [BaseExceptionGroup("unhandled errors in a TaskGroup", [error])],
        ) from None


async def test_a_wrapped_budget_is_not_reported_as_a_connection_failure(tmp_path):
    # A build phase that merely ran out of turns was surfacing as "connection,
    # provider or tool failed. Check your model ID, API credentials...", because
    # anyio had wrapped BudgetExceeded in two ExceptionGroups on the way out.
    run = Run(RunConfig(prompt="Create a lamp", auto_approve=True), tmp_path)
    await execute(run, MCPConfig(), Provider([action("get_scene_info")] * 5), task_group_connector)
    assert run.status == "budget_exhausted"
    assert "model turns" in run.events[-1]["message"]
    assert "API credentials" not in run.events[-1]["message"]


async def test_a_wrapped_cancellation_still_reads_as_cancelled(tmp_path):
    run = Run(RunConfig(prompt="Create a lamp", auto_approve=True), tmp_path)

    class Cancelling:
        async def complete(self, messages, tools):
            raise asyncio.CancelledError()

    await execute(run, MCPConfig(), Cancelling(), task_group_connector)
    assert run.status == "cancelled"


async def test_a_genuine_failure_leaves_a_redacted_traceback(tmp_path):
    class Broken:
        async def complete(self, messages, tools):
            raise RuntimeError("boom sk-secret-key-value")

    run = Run(RunConfig(prompt="Create a lamp", auto_approve=True, api_key="sk-secret-key-value"), tmp_path)
    await execute(run, MCPConfig(), Broken(), task_group_connector)
    assert run.status == "failed"
    log = (run.directory / "error.log").read_text(encoding="utf-8")
    assert "RuntimeError" in log
    assert "sk-secret-key-value" not in log
    assert "[REDACTED]" in log


async def test_a_stopped_run_saves_state_and_can_be_continued(tmp_path):
    # The model had a plan for the rest of the scene and simply ran out of
    # turns mid-build; there was no way to carry on from there.
    session = Session()

    @asynccontextmanager
    async def connector(_):
        yield session

    plan_then_build = [{"role": "assistant", "content": "Plan done"}] + [action()] * 5
    first = Run(
        RunConfig(prompt="Create a lamp", auto_approve=True, build_max_steps=1),
        tmp_path,
    )
    await execute(first, MCPConfig(), Provider(plan_then_build), connector)
    assert first.status == "budget_exhausted"

    saved = json.loads((first.directory / "state.json").read_text(encoding="utf-8"))
    assert saved["stage"] == "build"
    assert saved["steps"] == 2

    second = Run(RunConfig(prompt="Create a lamp", auto_approve=True), tmp_path)
    await execute(second, MCPConfig(), Provider(), connector, state=saved)
    assert second.status == "completed"
    # Resumed at build, so planning is not repeated, and the model is told the
    # scene already holds its earlier work.
    assert [e["phase"] for e in second.events if e["type"] == "phase"] == [
        "build",
        "review",
        "refine",
        "finalize",
    ]
    assert any("RESUMING" in str(m.get("content", "")) for m in second.messages)
    assert (second.directory / "scene.blend").exists()


async def test_saved_state_carries_no_images_and_no_key(tmp_path):
    session = Session()

    @asynccontextmanager
    async def connector(_):
        yield session

    run = Run(
        RunConfig(prompt="Create a lamp", auto_approve=True, api_key="sk-secret-key-value"),
        tmp_path,
    )
    await execute(run, MCPConfig(), Provider(), connector)
    body = (run.directory / "state.json").read_text(encoding="utf-8")
    assert "sk-secret-key-value" not in body
    # Image payloads are large and a resumed run re-captures the viewport.
    assert "image_url" not in body
    assert "data:image" not in body


async def test_zero_budgets_mean_no_limit(tmp_path):
    from astra_blender.engine import UNCAPPED, budget

    assert budget(0) == UNCAPPED
    assert budget(12) == 12

    session = Session()

    @asynccontextmanager
    async def connector(_):
        yield session

    # Nothing caps this run but the model choosing to finish each phase.
    run = Run(
        RunConfig(
            prompt="Create a lamp",
            auto_approve=True,
            max_steps=0,
            max_total_tokens=0,
            timeout_seconds=0,
        ),
        tmp_path,
    )
    await execute(run, MCPConfig(), Provider(), connector)
    assert run.status == "completed"
    assert run.deadline.when() is None
