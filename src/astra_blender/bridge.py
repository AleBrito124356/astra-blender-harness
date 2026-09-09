"""Keep the MCP context and all its calls in the owning asyncio task."""

from contextlib import asynccontextmanager
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .config import MCPConfig


@asynccontextmanager
async def connect(config: MCPConfig):
    if config.transport == "stdio":
        # The SDK inherits a minimal environment, not the LLM provider's credentials.
        async with stdio_client(
            StdioServerParameters(command=config.command, args=config.args, env=config.env)
        ) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=90)) as session:
                await session.initialize()
                yield session
    else:
        async with streamablehttp_client(config.url, headers=config.headers) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=90)) as session:
                await session.initialize()
                yield session


async def discover(session, allowed: list[str]) -> dict:
    found, cursor = {}, None
    while True:
        page = await session.list_tools(cursor=cursor)
        for tool in page.tools:
            if tool.name in allowed:
                found[tool.name] = tool
        cursor = page.nextCursor
        if not cursor:
            break
    required = {"get_scene_info", "execute_blender_code", "get_viewport_screenshot"}
    if missing := required - found.keys():
        raise ValueError(f"Blender MCP is missing required tools: {', '.join(sorted(missing))}")
    return found
