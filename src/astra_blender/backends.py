"""Ways to reach Blender, behind one session interface.

The engine, the live viewer and the MCP server only ever call
``list_tools()`` and ``call_tool(name, arguments)`` and read MCP
``CallToolResult`` objects back. Three backends provide that:

* ``socket`` - straight to the Blender MCP add-on's socket inside a running
  Blender (default 127.0.0.1:9876). No ``uvx``, no second MCP server process,
  and no dependence on which tool names the upstream server exposes this month.
* ``headless`` - a background Blender that Astra starts and owns: a Python
  interpreter with the ``bpy`` wheel, or a Blender executable. It runs
  ``headless_worker.py``, which speaks the add-on's socket protocol behind a
  per-process token. No GUI, so it works in CI, containers and cloud agents.
* ``stdio``/``http`` - an MCP server such as upstream Blender MCP. Version 2 of
  that server replaced ``get_viewport_screenshot`` with ``look``;
  :class:`CompatSession` maps one onto the other so either version works.

Command failures come back as ``isError`` results, never as exceptions: an
exception means the transport broke, and the live hub drops the connection on
one - which for a headless backend would throw the scene away.
"""

import asyncio
import base64
import importlib.util
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

from mcp.types import CallToolResult, ImageContent, TextContent, Tool

WORKER = Path(__file__).with_name("headless_worker.py")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
READ_CHUNK = 1 << 16

ADDON_TOOLS = {
    "get_scene_info": (
        "Basic facts about the open Blender scene: name, object count, first objects and their "
        "world locations. Use astra_inspect_scene for numerical evidence.",
        {"type": "object", "properties": {}},
    ),
    "get_object_info": (
        "Transform, visibility, parent, materials and mesh size of one object by exact name.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["name"],
        },
    ),
    "execute_blender_code": (
        "Run Python in Blender (bpy, bmesh, mathutils are importable). Whatever it prints is "
        "returned. Work in small steps and print what you need to know.",
        {
            "type": "object",
            "properties": {"code": {"type": "string", "minLength": 1, "maxLength": 200000}},
            "required": ["code"],
        },
    ),
    "get_viewport_screenshot": (
        "Capture the 3D viewport as an image (a headless Blender renders a small preview "
        "through the scene camera instead).",
        {
            "type": "object",
            "properties": {"max_size": {"type": "integer", "minimum": 64, "maximum": 2048}},
        },
    ),
}


def addon_tools():
    return [
        Tool(name=name, description=description, inputSchema=schema)
        for name, (description, schema) in ADDON_TOOLS.items()
    ]


def text_result(text, error=False):
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=error)


class BlenderUnavailable(RuntimeError):
    """Blender could not be started or reached."""


class AddonSession:
    """An MCP-shaped session over the Blender MCP add-on's socket protocol."""

    simulated = False

    def __init__(self, host="127.0.0.1", port=9876, *, token=None, timeout=180.0, label="socket"):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self.label = label
        self.reader = None
        self.writer = None
        self.lock = asyncio.Lock()
        self.info = {}
        self.shots = None

    async def open(self):
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=READ_CHUNK * 4), timeout=10
            )
        except (OSError, TimeoutError) as error:
            raise BlenderUnavailable(
                f"Cannot reach Blender at {self.host}:{self.port}. In Blender, enable the Blender MCP "
                "add-on and press its Connect/Start button in the 3D viewport sidebar, or use the "
                "headless backend."
            ) from error
        self.shots = Path(tempfile.mkdtemp(prefix="astra-shots-"))
        try:
            self.info = await self.command("get_addon_info", {})
        except BlenderCommandError:
            # Add-ons before the handshake command still serve everything Astra needs.
            self.info = {}
        return self

    async def _reconnect(self):
        if self.writer is not None:
            self.writer.close()
            with suppress(Exception):
                await self.writer.wait_closed()
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, limit=READ_CHUNK * 4), timeout=10
        )

    async def close(self):
        if self.writer is not None:
            self.writer.close()
            with suppress(Exception):
                await self.writer.wait_closed()
            self.writer = None
        if self.shots is not None:
            shutil.rmtree(self.shots, ignore_errors=True)
            self.shots = None

    async def initialize(self):
        return self.info

    async def command(self, kind, params, timeout=None):
        """Send one request and wait for its answer. Raises on transport failure."""
        if self.writer is None:
            raise ConnectionError("Blender session is closed")
        request = {"type": kind, "params": params}
        if self.token:
            request["token"] = self.token
        limit = timeout or self.timeout
        async with self.lock:
            self.writer.write(json.dumps(request).encode("utf-8"))
            await self.writer.drain()
            try:
                reply = await asyncio.wait_for(self._read_reply(), limit)
            except TimeoutError:
                # A late answer would be read as the reply to the next request.
                # Start a fresh connection; the old command may still finish.
                await self._reconnect()
                raise BlenderCommandError(
                    f"Blender did not answer within {limit:.0f} s. The operation may still be running "
                    "in Blender; inspect the scene before repeating it."
                )
        if not isinstance(reply, dict):
            raise ConnectionError("Blender sent a reply that is not a JSON object")
        if reply.get("status") == "error":
            raise BlenderCommandError(str(reply.get("message", "Blender reported an error")))
        result = reply.get("result")
        if isinstance(result, dict) and result.get("error") and len(result) <= 2:
            raise BlenderCommandError(str(result["error"]))
        return result

    async def _read_reply(self):
        """Read until the bytes form one JSON object.

        The protocol has no length prefix: a reply is complete when it parses.
        A geometry snapshot runs to megabytes, so parsing is attempted only
        when the data ends with a closing brace and the braces balance, or the
        stream has paused - not after every chunk.
        """
        data = bytearray()
        decoder = json.JSONDecoder()
        while True:
            try:
                chunk = await asyncio.wait_for(self.reader.read(READ_CHUNK), timeout=0.25)
                paused = False
            except TimeoutError:
                chunk, paused = b"", True
                if not data:
                    continue
            if not chunk and not paused:
                raise ConnectionError("Blender closed the connection")
            data.extend(chunk)
            tail = data.rstrip()
            if not tail.endswith(b"}"):
                continue
            if not paused and data.count(b"{") != data.count(b"}"):
                continue
            try:
                text = bytes(data).decode("utf-8")
                value, end = decoder.raw_decode(text.lstrip())
            except (UnicodeDecodeError, ValueError):
                continue
            return value

    async def list_tools(self, cursor=None):
        return SimpleNamespace(tools=addon_tools(), nextCursor=None)

    async def call_tool(self, name, arguments=None):
        arguments = dict(arguments or {})
        try:
            if name == "get_scene_info":
                result = await self.command("get_scene_info", {})
                return text_result(json.dumps(result, indent=2, ensure_ascii=False))
            if name == "get_object_info":
                result = await self.command("get_object_info", {"name": arguments.get("name", "")})
                return text_result(json.dumps(result, indent=2, ensure_ascii=False))
            if name == "execute_blender_code":
                return await self._execute(arguments.get("code", ""))
            if name == "get_viewport_screenshot":
                return await self._screenshot(int(arguments.get("max_size") or 800))
        except BlenderCommandError as error:
            return text_result(f"Error: {error}", error=True)
        return text_result(f"Error: unknown tool {name}", error=True)

    async def _execute(self, code):
        try:
            result = await self.command("execute_code", {"code": code})
        except BlenderCommandError as error:
            return text_result("Error executing code: " + describe_failure(str(error)), error=True)
        output = result.get("result", "") if isinstance(result, dict) else str(result or "")
        return text_result("Code executed successfully: " + output)

    async def _screenshot(self, max_size):
        if self.host not in LOOPBACK:
            return text_result(
                "Error: screenshots need Blender on this machine; the image file would be written "
                "on the remote host.",
                error=True,
            )
        path = self.shots / f"shot-{secrets.token_hex(6)}.png"
        result = await self.command(
            "get_viewport_screenshot",
            {"max_size": max(64, min(max_size, 2048)), "filepath": str(path), "format": "png"},
            timeout=max(self.timeout, 300),
        )
        try:
            data = path.read_bytes()
        except OSError:
            return text_result("Error: Blender reported a screenshot but no file was written", error=True)
        finally:
            path.unlink(missing_ok=True)
        note = ""
        if isinstance(result, dict) and result.get("method") == "render":
            note = (
                f"Headless preview rendered with {result.get('engine')} through the "
                f"{result.get('source', 'camera')} at {result.get('width')}x{result.get('height')}."
            )
        content = [
            ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/png")
        ]
        if note:
            content.append(TextContent(type="text", text=note))
        return CallToolResult(content=content, isError=False)


class BlenderCommandError(RuntimeError):
    """Blender answered, and the answer was an error."""


def describe_failure(message):
    """Render the add-on's JSON error payload as text a model can act on."""
    try:
        detail = json.loads(message)
    except ValueError:
        return message
    if not isinstance(detail, dict) or "traceback" not in detail:
        return message
    text = f"{detail.get('exception_type', 'Error')}: {detail.get('message', '')}\n\n{detail['traceback']}"
    if detail.get("output"):
        text += "\nOutput printed before the error:\n" + str(detail["output"])
    return text


# -- headless --------------------------------------------------------------

# Passed through to Blender. Everything else - API keys included - stays out of
# a process that runs model-written Python.
ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
    "TMPDIR",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "__EGL_VENDOR_LIBRARY_FILENAMES",
    "VIRTUAL_ENV",
)


def worker_environment(extra=None):
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(extra or {})
    return env


def classify(path):
    """'python' for an interpreter, 'blender' for a Blender executable."""
    name = Path(path).name.lower()
    if name.startswith("python") or name.startswith("pypy"):
        return "python"
    return "blender"


def find_blender(explicit=None):
    """Where a headless Blender comes from, and how it was found.

    Order: the configured path, $ASTRA_BLENDER, $BLENDER, `blender` on PATH,
    then this interpreter itself when the bpy wheel is installed in it.
    """
    for source, value in (
        ("configuration", explicit),
        ("ASTRA_BLENDER", os.environ.get("ASTRA_BLENDER")),
        ("BLENDER", os.environ.get("BLENDER")),
    ):
        if value:
            resolved = shutil.which(value) or (value if Path(value).is_file() else None)
            if not resolved:
                raise BlenderUnavailable(f"{source} names {value!r}, which does not exist")
            return resolved, classify(resolved), source
    on_path = shutil.which("blender")
    if on_path:
        return on_path, "blender", "PATH"
    if importlib.util.find_spec("bpy") is not None:
        return sys.executable, "python", "bpy installed in this interpreter"
    raise BlenderUnavailable(
        "No Blender for the headless backend. Install the bpy wheel in a Python that matches it "
        "(bpy 5.2 needs Python 3.13: `pip install bpy`) and set `blender` to that python, or "
        "install Blender and put `blender` on PATH (or set ASTRA_BLENDER)."
    )


def worker_command(path, kind, port=0, blend_file=None, preview_engine="auto"):
    args = ["--", "--port", str(port), "--preview-engine", preview_engine]
    if blend_file:
        args += ["--file", str(blend_file)]
    if kind == "python":
        return [path, "-u", str(WORKER), *args]
    return [
        path,
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python-exit-code",
        "1",
        "--python",
        str(WORKER),
        *args,
    ]


class HeadlessSession(AddonSession):
    def __init__(self, config):
        super().__init__(
            "127.0.0.1",
            0,
            token=secrets.token_urlsafe(24),
            timeout=float(config.command_timeout),
            label="headless",
        )
        self.config = config
        self.process = None
        self.output = deque(maxlen=60)
        self.drain_task = None
        self.started = None

    async def start(self):
        path, kind, source = find_blender(self.config.blender)
        command = worker_command(
            path, kind, blend_file=self.config.blend_file, preview_engine=self.config.preview_engine
        )
        env = worker_environment({**self.config.env, "ASTRA_WORKER_TOKEN": self.token})
        self.started = time.monotonic()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except OSError as error:
            raise BlenderUnavailable(f"Could not start {path}: {error}") from error
        ready = await self._wait_ready()
        _, port, *version = ready.split()
        self.port = int(port)
        self.info = {"blender_version": " ".join(version), "executable": path, "kind": kind, "source": source}
        self.drain_task = asyncio.create_task(self._drain())
        await self.open()
        self.info.update(
            {"executable": path, "kind": kind, "source": source, "session_id": secrets.token_hex(8)}
        )
        return self

    async def _wait_ready(self):
        deadline = time.monotonic() + max(30.0, float(self.config.startup_timeout))
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=1.0)
            except TimeoutError:
                continue
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            self.output.append(text)
            if text.startswith("ASTRA_WORKER_READY "):
                return text
            if text.startswith("ASTRA_WORKER_ERROR"):
                break
        await self._stop_process()
        tail = "\n".join(list(self.output)[-12:])
        raise BlenderUnavailable("The headless Blender did not start.\n" + tail)

    async def _drain(self):
        # Blender logs every render; an unread pipe would eventually block it.
        with suppress(Exception):
            while line := await self.process.stdout.readline():
                self.output.append(line.decode("utf-8", errors="replace").rstrip())

    async def _stop_process(self):
        if self.process is None or self.process.returncode is not None:
            return
        with suppress(Exception):
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except TimeoutError:
            with suppress(ProcessLookupError):
                self.process.kill()
            with suppress(Exception):
                await self.process.wait()

    async def close(self):
        if self.writer is not None:
            with suppress(Exception):
                self.writer.write(json.dumps({"type": "shutdown", "token": self.token}).encode())
                await asyncio.wait_for(self.writer.drain(), timeout=2)
        await super().close()
        await self._stop_process()
        if self.drain_task is not None:
            self.drain_task.cancel()
            await asyncio.gather(self.drain_task, return_exceptions=True)

    async def save_copy(self, filepath):
        return await self.command("save_as", {"filepath": str(filepath), "copy": True}, timeout=300)

    async def open_file(self, filepath):
        return await self.command("open_file", {"filepath": str(filepath)}, timeout=300)


async def socket_listening(host, port, timeout=0.5):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    return True


async def resolve_auto(config):
    """`auto`: the Blender on screen if its add-on is listening, else a headless one."""
    if config.transport != "auto":
        return config
    if await socket_listening(config.host, config.port):
        return config.model_copy(update={"transport": "socket"})
    try:
        find_blender(config.blender)
    except BlenderUnavailable:
        raise BlenderUnavailable(
            f"No Blender to work with: nothing is listening on {config.host}:{config.port} (start the "
            "Blender MCP add-on in Blender's sidebar) and no headless Blender is installed (pip install "
            "bpy in Python 3.13, or put `blender` on PATH, or set ASTRA_BLENDER)."
        ) from None
    return config.model_copy(update={"transport": "headless"})


@asynccontextmanager
async def connect_socket(config):
    session = AddonSession(config.host, config.port, timeout=float(config.command_timeout))
    await session.open()
    try:
        yield session
    finally:
        await session.close()


@asynccontextmanager
async def connect_headless(config):
    session = HeadlessSession(config)
    try:
        await session.start()
        yield session
    finally:
        await session.close()


# -- upstream MCP server compatibility ---------------------------------------


class CompatSession:
    """Present upstream Blender MCP 1.x and 2.x with the same tool names.

    Version 2 dropped ``get_viewport_screenshot`` for ``look(mode=...)``. When
    a server offers ``look`` but not the old name, the old name is listed as an
    alias and forwarded with ``mode="viewport"``.
    """

    simulated = False
    label = "mcp"

    def __init__(self, session):
        self.session = session
        self.seen = set()
        self.look = None
        self.aliased = False

    def __getattr__(self, name):
        return getattr(self.session, name)

    async def list_tools(self, cursor=None):
        page = await self.session.list_tools(cursor=cursor)
        for tool in page.tools:
            self.seen.add(tool.name)
            if tool.name == "look":
                self.look = tool
        tools = list(page.tools)
        if not page.nextCursor and self.look is not None and "get_viewport_screenshot" not in self.seen:
            self.aliased = True
            tools.append(
                Tool(
                    name="get_viewport_screenshot",
                    description="Capture the user's 3D viewport (Blender MCP 2 'look', viewport mode).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "max_size": {"type": "integer", "minimum": 200, "maximum": 2000},
                            "user_prompt": {"type": "string"},
                        },
                    },
                )
            )
        return SimpleNamespace(tools=tools, nextCursor=page.nextCursor)

    async def call_tool(self, name, arguments=None):
        if name == "get_viewport_screenshot" and self.aliased:
            arguments = dict(arguments or {})
            forwarded = {"mode": "viewport", "max_size": int(arguments.get("max_size") or 1000)}
            if "user_prompt" in ((self.look.inputSchema or {}).get("properties") or {}):
                forwarded["user_prompt"] = arguments.get("user_prompt", "")
            return await self.session.call_tool("look", forwarded)
        return await self.session.call_tool(name, arguments)
