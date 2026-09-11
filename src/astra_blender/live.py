"""One MCP owner task serializes agent commands and human 3D preview reads."""

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager

from .bridge import connect
from .spatial import diagnostics, parse_probe, probe_code


class BlenderHub:
    def __init__(self, config, connector=connect):
        self.config = config
        self.connector = connector
        self.queue = asyncio.Queue(maxsize=32)
        self.task = None
        self.operation = None
        self.connected = False
        self.closing = False

    @asynccontextmanager
    async def connection(self, config=None):
        yield self

    async def list_tools(self, cursor=None):
        return await self.request("list_tools", cursor=cursor)

    async def call_tool(self, name, arguments):
        if self.config and name not in self.config.allowed_tools:
            raise ValueError("Tool is not enabled in this MCP configuration")
        return await self.request("call_tool", name, arguments)

    async def request(self, method, *args, **kwargs):
        if self.closing:
            raise RuntimeError("Blender connection is closing")
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((future, method, args, kwargs))
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._owner())
        return await future

    async def _owner(self):
        current = None
        try:
            async with self.connector(self.config) as session:
                self.connected = True
                while True:
                    current, method, args, kwargs = await self.queue.get()
                    if current.cancelled():
                        continue
                    self.operation = args[0] if args else method
                    result = await getattr(session, method)(*args, **kwargs)
                    if not current.done():
                        current.set_result(result)
                    current = None
                    self.operation = None
        except (Exception, asyncio.CancelledError, BaseExceptionGroup):
            error = RuntimeError("Blender MCP disconnected. Inspect Blender before retrying a mutation.")
            if current and not current.done():
                current.set_exception(error)
            while not self.queue.empty():
                future, *_ = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(error)
        finally:
            self.connected = False
            self.operation = None

    async def close(self):
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class LiveScene:
    def __init__(self, hub):
        self.hub = hub
        self.snapshot = None
        self.revision = None
        self.captured_at = None
        self.attempted_at = 0.0
        self.refresh_task = None
        self.error = None

    async def _refresh(self):
        try:
            result = await self.hub.call_tool(
                "execute_blender_code",
                {
                    "code": probe_code(geometry=True),
                    "user_prompt": "Read the scene for the human live 3D viewer",
                },
            )
            snapshot = parse_probe(result)
            revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]
            self.snapshot, self.revision = snapshot, revision
            self.captured_at = time.time()
            self.error = None
        except Exception:
            self.error = "Cannot refresh the live scene. Check Blender and the MCP connection."
        finally:
            self.attempted_at = time.monotonic()

    async def read(self, revision=None):
        if not self.refresh_task or self.refresh_task.done():
            if time.monotonic() - self.attempted_at >= (5 if self.error else 1.5):
                self.refresh_task = asyncio.create_task(self._refresh())
        if self.snapshot is None and self.refresh_task and not self.refresh_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.refresh_task), timeout=1.0)
            except TimeoutError:
                pass
        return {
            "revision": self.revision,
            "captured_at": self.captured_at,
            "refreshing": bool(self.refresh_task and not self.refresh_task.done()),
            "operation": self.hub.operation,
            "error": self.error,
            "snapshot": self.snapshot if self.revision != revision else None,
            "diagnostics": diagnostics(self.snapshot)
            if self.snapshot and self.revision != revision
            else None,
        }

    async def close(self):
        if self.refresh_task:
            self.refresh_task.cancel()
            await asyncio.gather(self.refresh_task, return_exceptions=True)
