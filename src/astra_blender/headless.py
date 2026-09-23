"""A persistent headless Blender that answers like the upstream MCP tools.

HeadlessBlender runs `blender --background --factory-startup --python
headless_worker.py` and talks JSON lines over stdin/stdout. It never opens a
socket, so it cannot touch a live Blender (port 9876) or the Astra server.

headless_connect(config) is a drop-in connector for engine.execute: its
session lists the four upstream tools (data/upstream_tools.json, dumped from
blender-mcp 1.9.1) and returns their exact strings:

    execute_blender_code   "Code executed successfully: <stdout>"
                           "Error executing code: Communication error with
                            Blender: Code execution error: <error>"
                           "Rejected by safe mode - line N: ..." (checked on
                            this side with blender_mcp.safe_mode, never run)
    get_scene_info         the add-on's JSON: name, counts, first 10 objects
    get_object_info        the add-on's JSON, or its "Error getting object
                           info: ..." string
    get_viewport_screenshot  isError, as upstream fails without a viewport;
                           with screenshots=True, a small Workbench render
                           of the scene camera instead (a PNG image block),
                           so vision runs can be tested offline

Used by the tests (pytest -m blender), the benchmark and anyone who wants to
run the whole engine offline. Background Blender has no viewport, so a run
either turns vision off or asks for camera renders:

    async with headless_connect(MCPConfig()) as session: ...
    run = Run(RunConfig(prompt=..., vision=False), root)
    await execute(run, MCPConfig(), provider, connector=headless_connect)

    # a vision run: get_viewport_screenshot answers with a camera render
    connector = functools.partial(headless_connect, screenshots=True)
    await execute(Run(RunConfig(prompt=...), root), MCPConfig(), provider, connector=connector)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

DEFAULT_EXE = "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"
WORKER = Path(__file__).with_name("headless_worker.py")
TOOLS_FILE = Path(__file__).parent / "data" / "upstream_tools.json"
PREFIX = "ASTRA_BRIDGE:"
# Upstream's socket gives up after 180 s; a call here gets a little less by
# default so a stuck script is killed before anything upstream would notice.
DEFAULT_TIMEOUT = 170.0
STARTUP_TIMEOUT = 120.0
SAFE_MODE_NOTE = (
    "{env} is enabled: scripts may only import bpy, bmesh, "
    "mathutils, and pure-python stdlib modules. No eval/exec/open, no "
    "os/subprocess/network access, no handlers/timers/drivers, no class "
    "or property registration, and no loading of external .blend "
    "datablocks. Blender operators for rendering, saving, and "
    "import/export ARE allowed. Rewrite the script within these limits; "
    "only the user can disable safe mode."
)
TIMEOUT_TEXT = (
    "Error executing code: Timeout waiting for Blender response - try simplifying your request. "
    "(Headless Blender was restarted after {seconds:g} s; the scene is back to the factory startup.)"
)


def blender_exe():
    """The Blender executable: ASTRA_BLENDER_EXE, else the 5.2 default path."""
    return os.environ.get("ASTRA_BLENDER_EXE") or DEFAULT_EXE


def available(exe=None):
    return Path(exe or blender_exe()).is_file()


def safe_mode_rejection(code):
    """Upstream's exact rejection text for code safe mode refuses, else None."""
    from blender_mcp.safe_mode import SAFE_MODE_ENV, SandboxViolation, validate_code

    try:
        validate_code(code)
    except SandboxViolation as exc:
        return f"Rejected by safe mode - {exc}\n\n" + SAFE_MODE_NOTE.format(env=SAFE_MODE_ENV)
    return None


class HeadlessError(RuntimeError):
    """The worker could not start or stopped answering."""


class HeadlessBlender:
    """One background Blender process, restarted when a call times out.

    Every method is blocking; headless_connect runs them in a thread so the
    engine's event loop keeps running. Calls are serialized by a lock.
    """

    def __init__(self, exe=None, timeout=DEFAULT_TIMEOUT, startup_timeout=STARTUP_TIMEOUT):
        self.exe = exe or blender_exe()
        self.timeout = float(timeout)
        self.startup_timeout = float(startup_timeout)
        self.process = None
        self.version = None
        self.restarts = 0
        self.calls = 0
        self._lines = None
        self._tail = []
        self._lock = threading.RLock()
        self._next_id = 0

    # -- process lifetime -------------------------------------------------
    def start(self):
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return self
            if not available(self.exe):
                raise HeadlessError(f"Blender not found at {self.exe}; set ASTRA_BLENDER_EXE")
            self._lines = queue.Queue()
            self._tail = []
            self.process = subprocess.Popen(
                [self.exe, "--background", "--factory-startup", "--python", str(WORKER)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            reader = threading.Thread(target=self._pump, args=(self.process, self._lines), daemon=True)
            reader.start()
            try:
                ready = self._receive(self.startup_timeout)
            except TimeoutError:
                self._kill()
                raise HeadlessError(f"Headless Blender did not start within {self.startup_timeout:g} s") from None
            if ready is None or "ready" not in ready:
                tail = "\n".join(self._tail[-15:])
                self._kill()
                raise HeadlessError("Headless Blender did not start. Last output:\n" + tail)
            self.version = ready["ready"]
            return self

    def _pump(self, process, lines):
        for line in process.stdout:
            if PREFIX in line:
                lines.put(line.split(PREFIX, 1)[1])
            else:
                self._tail = (self._tail + [line.rstrip()])[-40:]
        lines.put(None)

    def _receive(self, timeout):
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if line is None:
            return None
        return json.loads(line)

    def _kill(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.kill()
            process.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def close(self):
        with self._lock:
            process = self.process
            if process is None:
                return
            try:
                if process.poll() is None:
                    process.stdin.write(json.dumps({"op": "quit"}) + "\n")
                    process.stdin.flush()
                    process.wait(timeout=30)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            self._kill()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # -- requests ---------------------------------------------------------
    def request(self, op, timeout=None, **fields):
        """Send one request and wait for its reply; on timeout, restart and raise TimeoutError."""
        with self._lock:
            self.start()
            self._next_id += 1
            payload = {"op": op, "id": self._next_id, **fields}
            try:
                self.process.stdin.write(json.dumps(payload) + "\n")
                self.process.stdin.flush()
            except OSError as error:
                self._kill()
                raise HeadlessError(f"Headless Blender stopped: {error}") from error
            self.calls += 1
            try:
                reply = self._receive(self.timeout if timeout is None else timeout)
            except TimeoutError:
                self._kill()
                self.restarts += 1
                raise
            if reply is None:
                tail = "\n".join(self._tail[-15:])
                self._kill()
                raise HeadlessError("Headless Blender exited during a call. Last output:\n" + tail)
            return reply

    def run(self, code, timeout=None):
        """Execute a script as the add-on does. Returns the worker's reply dict:
        {ok, stdout, error?, line?}, or {ok: False, rejected: text} when safe
        mode refuses it (then nothing is sent to Blender)."""
        rejection = safe_mode_rejection(code)
        if rejection is not None:
            return {"ok": False, "rejected": rejection, "stdout": ""}
        return self.request("exec", timeout=timeout, code=code)

    def reset(self):
        """Back to the factory startup scene (Cube, Camera, Light)."""
        return self.request("reset")["result"]

    # -- upstream-shaped strings -----------------------------------------
    def execute_blender_code(self, code, timeout=None):
        try:
            reply = self.run(code, timeout=timeout)
        except TimeoutError:
            return TIMEOUT_TEXT.format(seconds=self.timeout if timeout is None else timeout)
        except HeadlessError as error:
            # Upstream's wording when the add-on's socket dies mid-command. A
            # crashed Blender is restarted by the next call.
            first = str(error).splitlines()[0]
            return f"Error executing code: Connection to Blender lost: {first} (the next call restarts it)"
        if "rejected" in reply:
            return reply["rejected"]
        if reply["ok"]:
            return f"Code executed successfully: {reply['stdout']}"
        return f"Error executing code: Communication error with Blender: Code execution error: {reply['error']}"

    def get_scene_info(self, timeout=None):
        reply = self.request("scene_info", timeout=timeout)
        return json.dumps(reply["result"], indent=2)

    def get_object_info(self, name, timeout=None):
        reply = self.request("object_info", timeout=timeout, name=name)
        if not reply["ok"]:
            return f"Error getting object info: Communication error with Blender: {reply['error']}"
        return json.dumps(reply["result"], indent=2)

    def get_viewport_screenshot(self, timeout=None):
        reply = self.request("screenshot", timeout=timeout)
        return "Error executing tool get_viewport_screenshot: Screenshot failed: " + reply["result"]["error"]

    def camera_render(self, max_size=800, timeout=None):
        """(png bytes, None) of a quick Workbench render of the scene camera,
        or (None, error text). Every render setting is restored afterwards."""
        reply = self.request("camera_render", timeout=timeout, max_size=int(max_size))
        if not reply["ok"]:
            return None, reply["error"]
        return base64.b64decode(reply["result"]["png"]), None


def upstream_tools():
    """The four upstream tool definitions, as blender-mcp 1.9.1 lists them."""
    from mcp.types import Tool

    return [Tool(**entry) for entry in json.loads(TOOLS_FILE.read_text(encoding="utf-8"))]


class HeadlessSession:
    """The subset of mcp.ClientSession the engine uses, backed by HeadlessBlender.

    call_tool takes ClientSession.call_tool's full signature; a per-call
    read_timeout_seconds becomes that call's worker timeout (a call that
    passes it restarts the worker, as a stuck add-on would be abandoned).
    """

    simulated = False

    def __init__(self, blender, screenshots=False):
        self.blender = blender
        self.screenshots = screenshots
        self.calls = []

    async def list_tools(self, cursor=None):
        return SimpleNamespace(tools=upstream_tools(), nextCursor=None)

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None, progress_callback=None, *, meta=None):
        from mcp.types import CallToolResult, ImageContent, TextContent

        arguments = dict(arguments or {})
        self.calls.append((name, arguments))
        timeout = read_timeout_seconds.total_seconds() if read_timeout_seconds is not None else None

        def text(value, error=False):
            return CallToolResult(content=[TextContent(type="text", text=value)], isError=error)

        blender = self.blender
        if name == "execute_blender_code":
            return text(await asyncio.to_thread(blender.execute_blender_code, arguments.get("code", ""), timeout))
        if name == "get_scene_info":
            return text(await asyncio.to_thread(blender.get_scene_info, timeout))
        if name == "get_object_info":
            return text(await asyncio.to_thread(blender.get_object_info, arguments.get("object_name", ""), timeout))
        if name == "get_viewport_screenshot":
            if not self.screenshots:
                return text(await asyncio.to_thread(blender.get_viewport_screenshot, timeout), error=True)
            png, error = await asyncio.to_thread(blender.camera_render, arguments.get("max_size", 800), timeout)
            if png is None:
                return text("Error executing tool get_viewport_screenshot: Screenshot failed: " + error, error=True)
            data = base64.b64encode(png).decode("ascii")
            return CallToolResult(content=[ImageContent(type="image", data=data, mimeType="image/png")])
        return text(f"Unknown tool: {name}", error=True)


@asynccontextmanager
async def headless_connect(config=None, *, blender=None, reset=False, timeout=None, screenshots=False):
    """An engine connector backed by headless Blender.

    Pass `blender` to reuse a running HeadlessBlender (the tests share one
    per session, reset between tests); otherwise one is started for this
    connection, with config.blender_timeout as its per-call timeout, and
    closed after it. `timeout` overrides the per-call timeout either way.
    `reset` returns a shared one to the factory scene first. `screenshots`
    answers get_viewport_screenshot with a camera render (see the module
    docstring) instead of upstream's failure.
    """
    owned = blender is None
    if owned:
        configured = getattr(config, "blender_timeout", None)
        blender = HeadlessBlender(timeout=timeout or configured or DEFAULT_TIMEOUT)
    elif timeout is not None:
        blender.timeout = float(timeout)
    await asyncio.to_thread(blender.start)
    try:
        if reset:
            await asyncio.to_thread(blender.reset)
        yield HeadlessSession(blender, screenshots=screenshots)
    finally:
        if owned:
            await asyncio.to_thread(blender.close)
