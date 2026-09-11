import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

from astra_blender.live import BlenderHub, LiveScene
from astra_blender.spatial import diagnostics, parse_probe


def result(data):
    return CallToolResult(
        content=[
            TextContent(type="text", text="Code executed successfully: ASTRA_SCENE_JSON:" + json.dumps(data))
        ]
    )


async def test_hub_serializes_preview_and_mutations_in_one_owner():
    active, peak, owners, calls = 0, 0, set(), []

    class Session:
        async def call_tool(self, name, args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            owners.add(asyncio.current_task())
            await asyncio.sleep(0.005)
            calls.append(name)
            active -= 1
            return name

    @asynccontextmanager
    async def connector(config):
        owner = asyncio.current_task()
        yield Session()
        assert asyncio.current_task() is owner

    hub = BlenderHub(None, connector)
    try:
        assert await asyncio.gather(hub.call_tool("edit", {}), hub.call_tool("preview", {})) == [
            "edit",
            "preview",
        ]
        assert peak == 1 and len(owners) == 1 and calls == ["edit", "preview"]
    finally:
        await hub.close()


async def test_cancelled_edit_is_not_retried_and_preview_can_continue():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Session:
        async def call_tool(self, name, args):
            calls.append(name)
            if name == "edit":
                started.set()
                await release.wait()
            return name

    @asynccontextmanager
    async def connector(config):
        yield Session()

    hub = BlenderHub(None, connector)
    edit = asyncio.create_task(hub.call_tool("edit", {}))
    await started.wait()
    edit.cancel()
    with pytest.raises(asyncio.CancelledError):
        await edit
    preview = asyncio.create_task(hub.call_tool("preview", {}))
    release.set()
    assert await preview == "preview"
    assert calls == ["edit", "preview"]
    await hub.close()


async def test_disconnect_fails_pending_work_and_next_read_reconnects():
    attempts = 0

    class Session:
        async def call_tool(self, name, args):
            if name == "edit":
                raise RuntimeError("transport lost")
            return name

    @asynccontextmanager
    async def connector(config):
        nonlocal attempts
        attempts += 1
        yield Session()

    hub = BlenderHub(None, connector)
    with pytest.raises(RuntimeError, match="Inspect Blender"):
        await hub.call_tool("edit", {})
    assert await hub.call_tool("preview", {}) == "preview"
    assert attempts == 2
    await hub.close()


async def test_live_coalesces_reads_and_omits_unchanged_geometry():
    snapshot = {"schema_version": 1, "objects": [], "meshes": [], "camera": None}
    calls = 0

    async def call_tool(*args):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return result(snapshot)

    live = LiveScene(SimpleNamespace(call_tool=call_tool, operation=None))
    first, second = await asyncio.gather(live.read(), live.read())
    assert first["snapshot"] == snapshot and second["revision"] == first["revision"]
    assert calls == 1
    unchanged = await live.read(first["revision"])
    assert unchanged["snapshot"] is None
    live.attempted_at = 0
    snapshot["frame"] = 2
    await live.read(first["revision"])
    await live.refresh_task
    changed = await live.read(first["revision"])
    assert changed["snapshot"]["frame"] == 2
    assert changed["revision"] != first["revision"]
    await live.close()


def test_probe_parses_upstream_text_prefix():
    assert parse_probe(result({"schema_version": 1, "objects": []}))["objects"] == []
    with pytest.raises(ValueError):
        parse_probe(result({"schema_version": 7}))


def test_geometry_checks_find_cropped_and_contained_subjects():
    house = {
        "name": "House",
        "dimensions": [8, 6, 3],
        "bounds": [[-4, -3, 0], [4, 3, 3]],
        "materials": ["wall"],
        "camera": {"center_in_frame": True, "corners_in_frame": 3},
    }
    car = {
        "name": "Car",
        "dimensions": [4, 2, 1],
        "bounds": [[-2, -1, 0], [2, 1, 1]],
        "materials": ["paint"],
        "camera": {"center_in_frame": False, "corners_in_frame": 0},
    }
    report = diagnostics({"camera": {"name": "Camera"}, "objects": [house, car]})
    codes = {issue["code"]: issue for issue in report["issues"]}
    assert "Car" in codes["outside_frame"]["objects"]
    assert "House" in codes["cropped_bounds"]["objects"]
    assert codes["contained_bounds"]["pairs"] == [{"object": "Car", "inside": "House"}]


async def test_hub_respects_configured_tool_allowlist():
    from astra_blender.config import MCPConfig

    hub = BlenderHub(MCPConfig(allowed_tools=["get_scene_info"]))
    with pytest.raises(ValueError, match="not enabled"):
        await hub.call_tool("execute_blender_code", {"code": "print(1)"})
    assert hub.task is None
