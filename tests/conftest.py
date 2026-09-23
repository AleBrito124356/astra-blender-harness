"""Shared fakes and fixtures for the whole suite.

Everything here is also a fixture, so tests in subdirectories (tests/blender/)
can use it without importing this file:

    FakeSession      a Blender MCP session answering trusted scripts by the
                     entry function they call: {function_name: payload}
    ScriptedProvider a model that returns scripted replies and records prompts
    action()         one native tool-call reply
    run_with()       run the whole engine against a session and a provider
    token_headers()  the X-Astra-Token header of a TestClient session

Markers (registered in pyproject.toml):
    blender  runs headless Blender 5.2; skipped when it is not installed
    live     needs a disposable live Blender MCP session (ASTRA_LIVE_MCP_CONFIG)
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from astra_blender import headless as backend
from astra_blender.config import MCPConfig, RunConfig
from astra_blender.demo import DemoSession
from astra_blender.engine import Run, execute

MARKER = "ASTRA_SCENE_JSON:"


def marker_result(payload):
    """A CallToolResult shaped like upstream's answer to a trusted script."""
    return CallToolResult(
        content=[TextContent(type="text", text="Code executed successfully: " + MARKER + json.dumps(payload))]
    )


class FakeSession(DemoSession):
    """A Blender that never runs code.

    `payloads` maps a trusted script's entry function (e.g. 'astra_scene_probe')
    to the dict it should print; a callable value gets the arguments dict.
    Harness saves are faked by writing the file the save script names, so
    save checks pass. Other scripts get DemoSession's generic answers.
    """

    simulated = False

    def __init__(self, payloads=None, error=False):
        self.payloads = dict(payloads or {})
        self.calls = []
        self.error = error

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.error:
            return CallToolResult(content=[TextContent(type="text", text="Error: Blender unavailable")])
        code = args.get("code", "") if isinstance(args, dict) else ""
        if name == "execute_blender_code" and "save_as_mainfile" in code:
            import ast

            call = ast.parse(code).body[1].value
            path = next(ast.literal_eval(k.value) for k in call.keywords if k.arg == "filepath")
            Path(path).write_bytes(b"BLENDER-test-fixture")
        if name == "execute_blender_code":
            for function, payload in self.payloads.items():
                if f"json.dumps({function}(" in code:
                    return marker_result(payload(code) if callable(payload) else payload)
        return await super().call_tool(name, args)

    def codes(self, function=None):
        """Every script sent to execute_blender_code, optionally only one entry's."""
        return [
            args["code"]
            for name, args in self.calls
            if name == "execute_blender_code" and (function is None or f"json.dumps({function}(" in args["code"])
        ]


class ScriptedProvider:
    """Returns scripted assistant messages in order, then 'Phase done'."""

    def __init__(self, responses=None, tokens=10):
        self.responses = iter(responses or [])
        self.messages = []
        self.tools = []
        self.tokens = tokens

    async def complete(self, messages, tools):
        self.messages.append(json.loads(json.dumps(messages)))
        self.tools.append([tool["function"]["name"] for tool in tools])
        return next(self.responses, {"role": "assistant", "content": "Phase done"}), {"total_tokens": self.tokens}


def action(name="execute_blender_code", args=None, *more):
    """A native reply calling one tool, or several: action(n1, a1, n2, a2...)."""
    pairs = [(name, args)] + [(more[i], more[i + 1]) for i in range(0, len(more), 2)]
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": f"call-{index + 1}",
                "type": "function",
                "function": {
                    "name": tool,
                    "arguments": json.dumps(arguments if arguments is not None else {"code": "print('model mutation')"}),
                },
            }
            for index, (tool, arguments) in enumerate(pairs)
        ],
    }


async def run_with(tmp_path, provider=None, session=None, connector=None, **config):
    """Run the engine once; auto_approve unless the caller says otherwise."""
    session = session or FakeSession()

    @asynccontextmanager
    async def fixed(_):
        yield session

    run = Run(RunConfig(**{"prompt": "Create a lamp", "auto_approve": True, **config}), tmp_path)
    await execute(run, MCPConfig(), provider or ScriptedProvider(), connector or fixed)
    return run, session


def token_headers(client):
    return {"X-Astra-Token": client.get("/api/session").json()["token"]}


@pytest.fixture
def fake_session():
    return FakeSession


@pytest.fixture
def scripted_provider():
    return ScriptedProvider


@pytest.fixture(name="action")
def action_fixture():
    return action


@pytest.fixture(name="run_with")
def run_with_fixture():
    return run_with


@pytest.fixture(name="token_headers")
def token_headers_fixture():
    return token_headers


@pytest.fixture(scope="session")
def blender_exe():
    """Path to Blender 5.2 (ASTRA_BLENDER_EXE or the default install); skips if missing."""
    if not backend.available():
        pytest.skip(f"Blender not found at {backend.blender_exe()}; set ASTRA_BLENDER_EXE")
    return backend.blender_exe()


@pytest.fixture(scope="session")
def headless_blender(blender_exe):
    """One background Blender for the whole session (started on first use)."""
    blender = backend.HeadlessBlender(exe=blender_exe, timeout=120)
    blender.start()
    yield blender
    blender.close()


@pytest.fixture
def headless(headless_blender):
    """The shared headless Blender, reset to the factory scene for this test."""
    headless_blender.timeout = 120
    headless_blender.reset()
    return headless_blender
