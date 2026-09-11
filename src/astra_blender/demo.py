"""Deterministic offline demo. Never executes generated code or contacts Blender."""

import asyncio
import base64
import struct
import zlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

from mcp.types import CallToolResult, ImageContent, TextContent, Tool


def illustration():
    width, height = 720, 480
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            shade = int(20 + 14 * (1 - y / height))
            color = (shade, shade + 3, shade + 8)
            dx, dy = (x - 360) / 180, (y - 285) / 65
            if dx * dx + dy * dy < 1:
                color = (47, 55, 65)
            # A diagram of an amber sculpture, explicitly labelled as demo by the UI.
            dx, dy = (x - 360) / 103, (y - 207) / 117
            radius = dx * dx + dy * dy
            if 0.29 < radius < 1:
                light = max(0, min(1, (1 - dx - dy) / 3))
                color = (int(160 + light * 85), int(80 + light * 96), int(39 + light * 49))
            rows.extend(color)

    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    return png + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b"")


class DemoSession:
    simulated = True

    async def list_tools(self, cursor=None):
        tools = [
            Tool(
                name=name,
                description="Simulated " + name,
                inputSchema={
                    "type": "object",
                    "properties": {"code": {"type": "string"}} if name == "execute_blender_code" else {},
                    "required": ["code"] if name == "execute_blender_code" else [],
                },
            )
            for name in ["get_scene_info", "execute_blender_code", "get_viewport_screenshot"]
        ]
        return SimpleNamespace(tools=tools, nextCursor=None)

    async def call_tool(self, name, arguments):
        await asyncio.sleep(0.15)
        if name == "execute_blender_code" and "ASTRA_SCENE_JSON:" in arguments.get("code", ""):
            import json

            snapshot = {
                "schema_version": 1,
                "scene": "Demo fixture",
                "blender_version": "fixture",
                "objects": [],
                "meshes": [],
                "camera": {"name": "Fixture camera"},
                "warnings": [],
                "render": {},
                "capabilities": {},
            }
            return CallToolResult(
                content=[TextContent(type="text", text="ASTRA_SCENE_JSON:" + json.dumps(snapshot))]
            )
        if name == "get_viewport_screenshot":
            return CallToolResult(
                content=[
                    ImageContent(
                        type="image", mimeType="image/png", data=base64.b64encode(illustration()).decode()
                    )
                ]
            )
        return CallToolResult(
            content=[TextContent(type="text", text="DEMO: simulated operation; no Blender file created.")]
        )


@asynccontextmanager
async def demo_connect(config):
    yield DemoSession()


class DemoProvider:
    def __init__(self):
        self.turn = 0

    async def complete(self, messages, tools):
        await asyncio.sleep(0.35)
        descriptions = [
            "Art direction: an amber ring sculpture, dark plinth, soft studio lighting. This is an offline demonstration.",
            "Demo build: the illustration shows the intended silhouette and warm/cool contrast. No scene was generated.",
            "Demo critique: strengthen the contact shadow and keep the inner silhouette readable.",
            "Demo refinement: in a real run, the model would now edit Blender and inspect a fresh viewport.",
            "Demo complete. The trace and illustration are downloadable. No .blend or rendered image was produced.",
        ]
        text = descriptions[min(self.turn, len(descriptions) - 1)]
        self.turn += 1
        return {"role": "assistant", "content": text}, {"total_tokens": 0}
