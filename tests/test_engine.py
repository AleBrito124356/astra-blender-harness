import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from astra_blender.config import MCPConfig, RunConfig
from astra_blender.demo import DemoSession
from astra_blender.engine import Run, execute
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
