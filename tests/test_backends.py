"""The socket and headless backends, against a fake add-on and (when available) real Blender."""

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

from astra_blender import backends
from astra_blender.backends import (
    AddonSession,
    BlenderCommandError,
    BlenderUnavailable,
    find_blender,
    worker_environment,
)
from astra_blender.bridge import connect, discover
from astra_blender.config import MCPConfig
from astra_blender.demo import illustration

BIG = "x" * 3_000_000


class FakeAddon:
    """Speaks the Blender MCP add-on protocol: one JSON object per request, no framing."""

    def __init__(self):
        self.requests = []
        self.server = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        decoder, buffer = json.JSONDecoder(), ""
        try:
            while chunk := await reader.read(65536):
                buffer += chunk.decode()
                while buffer.strip():
                    try:
                        request, end = decoder.raw_decode(buffer.lstrip())
                    except ValueError:
                        break
                    buffer = buffer.lstrip()[end:]
                    self.requests.append(request)
                    reply = await self.answer(request)
                    if reply is None:
                        continue
                    data = json.dumps(reply).encode()
                    # Split the reply so the client has to reassemble it.
                    for start in range(0, len(data), 7000):
                        writer.write(data[start : start + 7000])
                        await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def answer(self, request):
        kind, params = request.get("type"), request.get("params", {})
        if kind == "get_addon_info":
            return {"status": "error", "message": "Unknown command type: get_addon_info"}
        if kind == "get_scene_info":
            return {"status": "success", "result": {"name": "Scene", "objects": [{"name": "Cube"}]}}
        if kind == "get_viewport_screenshot":
            Path(params["filepath"]).write_bytes(illustration())
            return {"status": "success", "result": {"success": True, "width": 720, "height": 480}}
        if kind == "execute_code":
            code = params["code"]
            if code == "big":
                return {"status": "success", "result": {"executed": True, "result": BIG + "{ unbalanced"}}
            if code == "slow":
                await asyncio.sleep(3)
                return {"status": "success", "result": {"executed": True, "result": "late"}}
            if code == "boom":
                detail = {
                    "exception_type": "NameError",
                    "message": "name 'x' is not defined",
                    "traceback": "TB",
                }
                return {"status": "error", "message": json.dumps(detail)}
            return {"status": "success", "result": {"executed": True, "result": "ran: " + code + "\n"}}
        return {"status": "error", "message": f"Unknown command type: {kind}"}


async def test_socket_backend_speaks_the_addon_protocol():
    async with FakeAddon() as addon:
        config = MCPConfig(port=addon.port)
        async with connect(config) as session:
            tools = await discover(session, config.allowed_tools)
            assert set(tools) == {
                "get_scene_info",
                "get_object_info",
                "execute_blender_code",
                "get_viewport_screenshot",
            }
            scene = await session.call_tool("get_scene_info", {})
            assert json.loads(scene.content[0].text)["objects"][0]["name"] == "Cube"
            result = await session.call_tool("execute_blender_code", {"code": "print(1)"})
            assert not result.isError
            assert result.content[0].text == "Code executed successfully: ran: print(1)\n"
            failure = await session.call_tool("execute_blender_code", {"code": "boom"})
            assert failure.isError
            assert failure.content[0].text.startswith(
                "Error executing code: NameError: name 'x' is not defined"
            )
            shot = await session.call_tool("get_viewport_screenshot", {"max_size": 300})
            assert shot.content[0].type == "image"
            assert base64.b64decode(shot.content[0].data) == illustration()
            temporary = session.shots
            assert not any(temporary.iterdir()), "screenshots are deleted after reading"
        assert not temporary.exists()
        sent = [r for r in addon.requests if r["type"] == "get_viewport_screenshot"][0]
        assert sent["params"]["max_size"] == 300 and sent["params"]["format"] == "png"


async def test_multi_megabyte_replies_with_unbalanced_braces_in_strings():
    async with FakeAddon() as addon:
        async with connect(MCPConfig(port=addon.port)) as session:
            result = await session.call_tool("execute_blender_code", {"code": "big"})
            assert result.content[0].text.endswith(BIG + "{ unbalanced")


async def test_a_timeout_resets_the_connection_instead_of_desyncing_it():
    async with FakeAddon() as addon:
        async with connect(MCPConfig(port=addon.port, command_timeout=5)) as session:
            session.timeout = 1
            slow = await session.call_tool("execute_blender_code", {"code": "slow"})
            assert slow.isError and "did not answer within 1 s" in slow.content[0].text
            # The late reply to "slow" must not be read as the answer to this.
            nxt = await session.call_tool("execute_blender_code", {"code": "next"})
            assert nxt.content[0].text == "Code executed successfully: ran: next\n"


async def test_unreachable_blender_explains_what_to_start():
    with pytest.raises(BlenderUnavailable, match="Blender MCP add-on"):
        async with connect(MCPConfig(port=1)):
            pass


async def test_remote_hosts_get_no_screenshots():
    session = AddonSession("192.0.2.10", 9876)
    result = await session._screenshot(400)
    assert result.isError and "remote host" in result.content[0].text


def test_api_keys_never_reach_the_blender_process(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("ASTRA_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = worker_environment({"ASTRA_WORKER_TOKEN": "t" * 24})
    assert "OPENAI_API_KEY" not in env and "ASTRA_API_KEY" not in env
    assert env["PATH"] == "/usr/bin" and env["ASTRA_WORKER_TOKEN"] == "t" * 24


def test_find_blender_reports_a_wrong_configured_path(tmp_path):
    with pytest.raises(BlenderUnavailable, match="does not exist"):
        find_blender(str(tmp_path / "nope" / "blender"))


def test_worker_commands_for_both_kinds():
    python = backends.worker_command("/venv/bin/python3.13", "python", blend_file="/tmp/a.blend")
    assert python[:2] == ["/venv/bin/python3.13", "-u"] and python[-2:] == ["--file", "/tmp/a.blend"]
    blender = backends.worker_command("/opt/blender/blender", "blender")
    for flag in ("--background", "--factory-startup", "--disable-autoexec"):
        assert flag in blender
    assert backends.classify("/venv/bin/python3.13") == "python"
    assert backends.classify("C:/Program Files/Blender Foundation/Blender 5.2/blender.exe") == "blender"


# -- real Blender ------------------------------------------------------------


async def test_headless_worker_round_trip(tmp_path, blender_config):
    if blender_config.transport != "headless":
        pytest.skip("headless-specific")
    async with connect(blender_config) as session:
        assert session.info["blender_version"]
        result = await session.call_tool(
            "execute_blender_code",
            {"code": "import bpy\nbpy.ops.mesh.primitive_torus_add()\nprint(len(bpy.data.objects))"},
        )
        assert result.content[0].text == "Code executed successfully: 4\n"
        failed = await session.call_tool("execute_blender_code", {"code": "print('half')\n1/0"})
        assert failed.isError and "ZeroDivisionError" in failed.content[0].text
        assert "half" in failed.content[0].text
        shot = await session.call_tool("get_viewport_screenshot", {"max_size": 160})
        assert shot.content[0].type == "image"
        assert base64.b64decode(shot.content[0].data)[:8] == b"\x89PNG\r\n\x1a\n"
        copy = tmp_path / "copy.blend"
        await session.save_copy(copy)
        assert copy.stat().st_size > 1000
        process = session.process
        info = await session.command("get_scene_info", {})
        assert info["object_count"] == 4
    assert process.returncode is not None, "the worker exits with its session"


async def test_headless_worker_rejects_requests_without_its_token(blender_config):
    if blender_config.transport != "headless":
        pytest.skip("headless-specific")
    async with connect(blender_config) as session:
        # Free the worker's connection without stopping it, then knock without the token.
        session.writer.close()
        await session.writer.wait_closed()
        intruder = AddonSession("127.0.0.1", session.port)
        intruder.reader, intruder.writer = await asyncio.open_connection("127.0.0.1", session.port)
        with pytest.raises(BlenderCommandError, match="Invalid worker token"):
            await intruder.command("execute_code", {"code": "print('pwned')"})
        intruder.writer.close()
        await intruder.writer.wait_closed()
        await session._reconnect()
        assert (await session.command("ping", {}))["pong"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process check")
async def test_headless_worker_exits_when_astra_disappears(blender_config):
    if blender_config.transport != "headless":
        pytest.skip("headless-specific")
    session = backends.HeadlessSession(blender_config)
    await session.start()
    try:
        session.process.stdin.close()
        await asyncio.wait_for(session.process.wait(), 15)
    finally:
        await session.close()
