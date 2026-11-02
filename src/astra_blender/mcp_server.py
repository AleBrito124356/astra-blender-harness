"""Astra as an MCP server: guarded Blender tools for any agent.

`astra-blender mcp` puts Astra between an agent you already use - Claude Code,
Codex, Cursor, Claude Desktop, any MCP client - and Blender. The agent keeps
its own model, subscription and approval prompts; Astra contributes what raw
Blender access lacks:

* every model-written script passes Astra Guard before it reaches Blender;
* every edit is followed by a numerical audit, reported as a delta: which
  problems the edit introduced and which it fixed;
* reviewed tools for the operations models get wrong most often - camera
  framing, assembling parts, resting things on the ground, keyframes, motion
  checks - and Blender Quality Lab's gates and fixes when it is installed;
* verified scene checkpoints and restore;
* the live 3D viewer, so a person can watch the scene while the agent works.

It runs over stdio. Nothing in it prints to stdout except the protocol.
"""

import asyncio
import json
import re
import socket
import sys
import time
from contextlib import suppress
from pathlib import Path

from jsonschema import ValidationError, validate
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ImageContent,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
    Tool,
    ToolAnnotations,
)

from . import __version__, guard, motion, quality, spatial
from .bridge import discover
from .engine import result_parts
from .live import BlenderHub, LiveScene

LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
MAX_OBJECTS = 80

INSTRUCTIONS = """Astra gives you a guarded Blender. Workflow that produces good scenes:
1. blender_status, then inspect_scene: know the Blender version, the objects, their real sizes and the camera.
2. Plan 3-6 acceptance criteria (subject, scale, composition, materials, light, deliverable).
3. Build with run_python in small steps. Each call returns what your script printed AND an audit
   of the scene after the edit: new and resolved problems. Read it before the next step.
4. Use the reviewed tools instead of hand-written math: frame_camera for framing, assemble_parts
   and place_on_ground for multi-part objects, keyframe_object and inspect_animation for motion.
5. checkpoint before risky edits; restore_checkpoint if an edit goes wrong.
6. Finish with quality_check (when available) or inspect_scene, and look if you can see images.
Numbers are evidence, not taste: report remaining limitations honestly. Scripts may only import
bpy, bmesh, mathutils and plain-Python modules; Astra saves scene files itself."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
EDITS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
SAFE_EDIT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

TRUSTED = {
    "inspect_scene": "astra_inspect_scene",
    "frame_camera": "astra_frame_camera",
    "assemble_parts": "astra_assemble_parts",
    "place_on_ground": "astra_place_on_ground",
    "keyframe_object": "astra_keyframe_object",
    "inspect_animation": "astra_inspect_animation",
}


def _rename(text):
    for public, internal in TRUSTED.items():
        text = text.replace(internal, public)
    return text


def tool_definitions(quality_available):
    trusted = {tool.name: tool for tool in spatial.tools()}
    definitions = [
        Tool(
            name="blender_status",
            title="Blender status",
            description="Connection, Blender version, backend, guard, checkpoints and the live viewer URL. "
            "Call first; it also starts a headless Blender when that backend is configured.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=READ_ONLY,
        ),
        Tool(
            name="inspect_scene",
            title="Inspect scene numerically",
            description="Evaluated world bounds, dimensions, camera framing of each object, materials, the "
            "active camera and render settings, actual Blender API capabilities, and detected problems "
            "(cropped or off-frame subjects, missing materials, parts below the ground or detached). "
            "No images, no changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "objects": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "maxItems": 100,
                        "description": "Only report these objects in detail (problems still cover the scene).",
                    }
                },
                "additionalProperties": False,
            },
            annotations=READ_ONLY,
        ),
        Tool(
            name="run_python",
            title="Run Python in Blender",
            description="Run a Python script inside Blender (bpy, bmesh, mathutils, math, random, json, array "
            "and plain-Python modules). Returns what it printed, then an audit of the scene after the edit: "
            "problems it introduced and problems it fixed. Work in small steps. Astra Guard refuses "
            "scripts that import os/sys/subprocess, use eval/exec/open, touch interpreter internals, "
            "register handlers or classes, or open/save .blend files (use checkpoint).",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "minLength": 1, "maxLength": 200000},
                    "intent": {
                        "type": "string",
                        "maxLength": 300,
                        "description": "One line: what this step is for. Logged with the checkpoint trail.",
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
            annotations=EDITS,
        ),
        Tool(
            name="look",
            title="Look at the scene",
            description="An image of the scene: the 3D viewport of a desktop Blender, or a quick preview "
            "rendered through the scene camera by a headless Blender. Pixels are not proof of geometry; "
            "use inspect_scene for numbers.",
            inputSchema={
                "type": "object",
                "properties": {"max_size": {"type": "integer", "minimum": 128, "maximum": 2048}},
                "additionalProperties": False,
            },
            annotations=READ_ONLY,
        ),
        Tool(
            name="checkpoint",
            title="Save a checkpoint",
            description="Save a verified copy of the current scene in this session's folder. The open file "
            "is never overwritten. Use before risky edits.",
            inputSchema={
                "type": "object",
                "properties": {"label": {"type": "string", "pattern": LABEL.pattern}},
                "required": ["label"],
                "additionalProperties": False,
            },
            annotations=SAFE_EDIT,
        ),
        Tool(
            name="restore_checkpoint",
            title="Restore a checkpoint",
            description="Replace the open scene with a checkpoint saved in this session (by label). Unsaved "
            "changes since then are lost; in a desktop Blender the window reloads that copy.",
            inputSchema={
                "type": "object",
                "properties": {"label": {"type": "string", "pattern": LABEL.pattern}},
                "required": ["label"],
                "additionalProperties": False,
            },
            annotations=EDITS,
        ),
    ]
    for public, internal in TRUSTED.items():
        if public == "inspect_scene":
            continue
        tool = trusted[internal]
        definitions.append(
            Tool(
                name=public,
                title=public.replace("_", " ").capitalize(),
                description=_rename(tool.description or public),
                inputSchema=tool.inputSchema,
                annotations=READ_ONLY if internal in {"astra_inspect_animation"} else EDITS,
            )
        )
    if quality_available:
        definitions.append(
            Tool(
                name="quality_check",
                title="Quality check (Blender Quality Lab)",
                description=quality.DESCRIPTION,
                inputSchema=quality.SCHEMA,
                annotations=READ_ONLY,
            )
        )
    return definitions


def issue_key(issue):
    subjects = issue.get("objects") or [
        f"{pair.get('object')}<{pair.get('inside')}" for pair in issue.get("pairs", [])
    ]
    return issue.get("code"), tuple(subjects)


def describe_issue(issue):
    subjects = issue.get("objects") or [
        f"{pair.get('object')} inside {pair.get('inside')}" for pair in issue.get("pairs", [])[:6]
    ]
    extras = [f"{key}={issue[key]}" for key in ("depth", "gap", "ground") if key in issue]
    text = f"- {issue.get('level', 'info')} {issue.get('code')}"
    if subjects:
        text += ": " + ", ".join(str(s) for s in subjects[:8]) + (" ..." if len(subjects) > 8 else "")
    if extras:
        text += " (" + ", ".join(extras) + ")"
    return text + " - " + _rename(issue.get("message", ""))


class AstraServer:
    """Session state for one MCP client: the Blender hub, checkpoints and the last audit."""

    def __init__(self, config, output, viewer_port=8765, hub=None):
        self.config = config
        self.output = Path(output).resolve()
        self.session_dir = self.output / ("agent-" + time.strftime("%Y%m%d-%H%M%S"))
        self.hub = hub or BlenderHub(config)
        self.live = LiveScene(self.hub)
        self.viewer_port = viewer_port
        self.viewer_url = None
        self.viewer = None
        self.viewer_task = None
        self.ready = None
        self.issues = None
        self.checkpoints = {}
        self.edits = 0
        self.looks = 0
        self.lab = quality.lab()

    # -- plumbing ----------------------------------------------------------------

    async def ensure_ready(self):
        """Connect (or start the headless Blender) and learn the tool names once."""
        if self.ready is None:
            self.ready = await discover(self.hub, self.config.allowed_tools)
            self.session_dir.mkdir(parents=True, exist_ok=True)
        return self.ready

    async def execute(self, code):
        await self.ensure_ready()
        return await self.hub.call_tool("execute_blender_code", {"code": code})

    async def audit(self):
        result = await self.execute(spatial.probe_code())
        text, _ = result_parts(result)
        if text.startswith("TOOL ERROR:"):
            raise RuntimeError(text)
        report = spatial.diagnostics(spatial.parse_probe(result))
        (self.session_dir / "quality.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    def delta(self, report):
        before = {issue_key(i): i for i in (self.issues or [])}
        after = {issue_key(i): i for i in report["issues"]}
        self.issues = report["issues"]
        new = [after[k] for k in after if k not in before]
        fixed = [before[k] for k in before if k not in after]
        kept = [after[k] for k in after if k in before]
        camera = report.get("camera") or {}
        lines = [
            f"After-edit audit: {len(report['objects'])} objects, camera "
            + (repr(camera.get("name")) if camera else "none")
            + f". {len(new)} new, {len(fixed)} resolved, {len(kept)} still open."
        ]
        if new:
            lines += ["New:"] + [describe_issue(i) for i in new]
        if fixed:
            lines += ["Resolved:"] + [describe_issue(i) for i in fixed]
        if kept:
            lines.append("Still open: " + "; ".join(f"{i['code']}" for i in kept[:12]))
        return "\n".join(lines)

    async def save_copy(self, path):
        code = (
            "import bpy\nbpy.ops.wm.save_as_mainfile(filepath="
            + repr(Path(path).as_posix())
            + ", copy=True)\nprint('Saved scene copy')"
        )
        result = await self.execute(code)
        text, _ = result_parts(result)
        if text.startswith("TOOL ERROR:") or not Path(path).is_file():
            raise RuntimeError(
                "Blender did not write the checkpoint to "
                + str(path)
                + ". A remote Blender must share this folder at the same path."
            )

    # -- tools ---------------------------------------------------------------------

    async def call(self, name, arguments):
        try:
            return await self._call(name, arguments or {})
        except Exception as error:
            return error_result(f"{type(error).__name__}: {error}")

    async def _call(self, name, arguments):
        if name == "blender_status":
            return await self.status()
        if name == "inspect_scene":
            return await self.inspect_scene(arguments.get("objects"))
        if name == "run_python":
            return await self.run_python(arguments["code"], arguments.get("intent", ""))
        if name == "look":
            return await self.look(arguments.get("max_size", 768))
        if name == "checkpoint":
            return await self.checkpoint(arguments["label"])
        if name == "restore_checkpoint":
            return await self.restore(arguments["label"])
        if name == "quality_check" and self.lab is not None:
            return await self.quality_check(arguments)
        if name in TRUSTED:
            return await self.trusted(name, arguments)
        return error_result(f"Unknown tool {name}")

    async def status(self):
        tools = await self.ensure_ready()
        result = await self.execute(
            "import bpy\nimport json\nscene = bpy.context.scene\nprint(json.dumps({"
            "'blender': bpy.app.version_string, 'file': bpy.data.filepath, 'scene': scene.name, "
            "'objects': len(scene.objects), 'camera': scene.camera.name if scene.camera else None, "
            "'engine': scene.render.engine, 'frames': [scene.frame_start, scene.frame_end], "
            "'background': bpy.app.background}))"
        )
        text, _ = result_parts(result)
        if text.startswith("TOOL ERROR:"):
            return error_result(text)
        facts = json.loads(text.split("successfully:", 1)[-1].strip().splitlines()[-1])
        facts.update(
            {
                "backend": self.hub.backend or self.config.transport,
                "guard": "on" if self.config.guard else "off",
                "tools": sorted(tools),
                "session_folder": str(self.session_dir),
                "checkpoints": sorted(self.checkpoints),
                "viewer": self.viewer_url or "off",
                "quality_lab": "available" if self.lab else "not installed (pip install blender-quality-lab)",
                "astra": __version__,
            }
        )
        return text_result(json.dumps(facts, indent=2))

    async def inspect_scene(self, names=None):
        report = await self.audit()
        self.issues = report["issues"]
        objects = report["objects"]
        if names:
            wanted = set(names)
            objects = [o for o in objects if o["name"] in wanted]
            missing = sorted(wanted - {o["name"] for o in objects})
            if missing:
                report["missing_objects"] = missing
        if len(objects) > MAX_OBJECTS:
            report["truncated"] = f"{len(objects) - MAX_OBJECTS} more objects; pass objects=[...] to focus"
            objects = objects[:MAX_OBJECTS]
        report["objects"] = objects
        return text_result(_rename(json.dumps(report, ensure_ascii=False)))

    async def run_python(self, code, intent=""):
        if self.config.guard:
            try:
                guard.validate(code)
            except guard.GuardRejected as error:
                return error_result(guard.rejection_text(error).removeprefix("TOOL ERROR: "))
        await self.ensure_ready()
        if self.edits == 0 and "start" not in self.checkpoints:
            # The first edit of a session is the one people most want to undo.
            with suppress(Exception):
                await self._checkpoint("start")
        if self.issues is None:
            with suppress(Exception):
                self.issues = (await self.audit())["issues"]
        self.edits += 1
        result = await self.hub.call_tool("execute_blender_code", {"code": code})
        text, _ = result_parts(result)
        failed = text.startswith("TOOL ERROR:")
        body = text.removeprefix("TOOL ERROR: ")
        body = body.removeprefix("Code executed successfully: ") if not failed else body
        with (self.session_dir / "edits.jsonl").open("a", encoding="utf-8") as log:
            log.write(
                json.dumps({"time": time.time(), "intent": intent, "failed": failed, "code": code}) + "\n"
            )
        parts = [
            ("Python raised an error. Earlier lines may already have changed the scene:\n" + body)
            if failed
            else ("Output:\n" + (body.strip() or "(nothing printed)"))
        ]
        try:
            parts.append(self.delta(await self.audit()))
        except Exception as error:
            parts.append(f"After-edit audit unavailable: {error}")
        return text_result("\n\n".join(parts), error=failed)

    async def look(self, max_size):
        await self.ensure_ready()
        result = await self.hub.call_tool("get_viewport_screenshot", {"max_size": int(max_size)})
        for block in result.content:
            if isinstance(block, ImageContent) or getattr(block, "type", None) == "image":
                self.looks += 1
                with suppress(Exception):
                    import base64

                    suffix = "png" if block.mimeType == "image/png" else "jpg"
                    path = self.session_dir / f"look-{self.looks:03}.{suffix}"
                    path.write_bytes(base64.b64decode(block.data))
        return result

    async def _checkpoint(self, label):
        path = self.session_dir / f"{len(self.checkpoints):03}-{label}.blend"
        await self.save_copy(path)
        self.checkpoints[label] = path
        return path

    async def checkpoint(self, label):
        if not LABEL.match(label):
            return error_result("Use a label of letters, digits, '-' and '_' (max 40).")
        await self.ensure_ready()
        path = await self._checkpoint(label)
        return text_result(f"Checkpoint '{label}' saved: {path} ({path.stat().st_size} bytes)")

    async def restore(self, label):
        path = self.checkpoints.get(label)
        if path is None or not path.is_file():
            known = ", ".join(sorted(self.checkpoints)) or "none yet"
            return error_result(f"No checkpoint '{label}' in this session. Saved: {known}")
        code = (
            "import bpy\nbpy.ops.wm.open_mainfile(filepath="
            + repr(path.as_posix())
            + ", load_ui=False, use_scripts=False)\nprint(len(bpy.context.scene.objects))"
        )
        result = await self.execute(code)
        text, _ = result_parts(result)
        if text.startswith("TOOL ERROR:"):
            return error_result(text)
        self.issues = None
        report = await self.audit()
        self.issues = report["issues"]
        return text_result(
            f"Restored '{label}' from {path}. The scene now has {len(report['objects'])} geometry objects and "
            f"{len(report['issues'])} open problems."
        )

    async def trusted(self, name, arguments):
        internal = TRUSTED[name]
        if name == "inspect_scene":
            return await self.inspect_scene(arguments.get("objects"))
        tool = {t.name: t for t in spatial.tools()}[internal]
        try:
            validate(arguments, tool.inputSchema)
        except ValidationError as error:
            return error_result("Invalid arguments: " + error.message[:500])
        result = await self.execute(spatial.tool_code(internal, arguments))
        text, _ = result_parts(result)
        if text.startswith("TOOL ERROR:"):
            return error_result(_rename(text.removeprefix("TOOL ERROR: ")))
        if internal == "astra_inspect_animation":
            report = motion.report(spatial.parse_probe(result), spatial.diagnostics)
            (self.session_dir / "animation.json").write_text(json.dumps(report), encoding="utf-8")
            return text_result(_rename(json.dumps(report, ensure_ascii=False)))
        output = text.removeprefix("Code executed successfully: ").strip()
        try:
            audit = self.delta(await self.audit())
        except Exception as error:
            audit = f"After-edit audit unavailable: {error}"
        return text_result(_rename(output) + "\n\n" + audit)

    async def quality_check(self, arguments):
        result = await self.execute(quality.code(self.lab, arguments))
        text, _ = result_parts(result)
        if text.startswith("TOOL ERROR:"):
            hint = (
                quality.SAFE_MODE_HINT
                if (self.hub.backend or self.config.transport) in {"mcp", "stdio", "http"}
                else ""
            )
            return error_result(text.removeprefix("TOOL ERROR: ") + hint)
        summary, inspection, report = quality.summary(self.lab, text, bool(arguments.get("strict_contact")))
        (self.session_dir / "inspection.json").write_text(json.dumps(inspection), encoding="utf-8")
        (self.session_dir / "technical.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return text_result(_rename(summary))

    # -- viewer ----------------------------------------------------------------------

    async def start_viewer(self):
        if self.viewer_port is None:
            return None
        import uvicorn

        from .server import create_app

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", self.viewer_port))
        except OSError:
            listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_app(self.config, self.output, hub=self.hub, live_scene=self.live)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        self.viewer_url = f"http://127.0.0.1:{port}"
        self.viewer = server
        self.viewer_task = asyncio.create_task(server.serve(sockets=[listener]))
        print(f"Astra live viewer: {self.viewer_url}", file=sys.stderr, flush=True)
        return self.viewer_url

    async def close(self):
        if self.viewer_task is not None:
            self.viewer.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(self.viewer_task), 5)
            except (TimeoutError, Exception):
                self.viewer_task.cancel()
                await asyncio.gather(self.viewer_task, return_exceptions=True)
        await self.live.close()
        await self.hub.close()


def text_result(text, error=False):
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=error)


def error_result(text):
    return text_result(text, error=True)


PROMPT = Prompt(
    name="build_scene",
    title="Build a Blender scene",
    description="Astra's plan, build, review and refine workflow for a brief.",
    arguments=[
        PromptArgument(name="brief", description="What to create", required=True),
        PromptArgument(name="quality", description="draft, studio or final", required=False),
    ],
)


def build_prompt(brief, quality="studio", quality_available=False):
    steps = [
        "Call blender_status and inspect_scene. Preserve existing work unless the brief says otherwise.",
        "Write a short art direction and 3-6 concrete acceptance criteria (subject, real-world scale, "
        "composition, materials, light, deliverable).",
        "Build in small run_python steps inside a dedicated ASTRA collection, with meaningful object and "
        "material names, real-world scale, shared materials and deliberate bevels. Read each after-edit audit.",
        "Frame with frame_camera using exact subject names (not floors or backdrops).",
        "Review: inspect_scene"
        + (", quality_check" if quality_available else "")
        + " and look if you can see images. "
        "List concrete problems in composition, geometry, materials and light.",
    ]
    if quality != "draft":
        steps.append("Refine the largest problems first, then re-check. Checkpoint before risky changes.")
    if quality == "final":
        steps.append(
            "Render a final image with modest settings (EEVEE, or Cycles with low samples) to a path you "
            "report; never start an unbounded render."
        )
    steps.append(
        "Finish with what exists, what each acceptance criterion's evidence is, and remaining limits."
    )
    numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
    return f"Build this in Blender with the Astra tools.\n\nBRIEF: {brief}\nQUALITY: {quality}\n\n{numbered}"


def create_server(astra: AstraServer):
    server = Server("astra-blender", version=__version__, instructions=INSTRUCTIONS)
    definitions = tool_definitions(astra.lab is not None)

    @server.list_tools()
    async def list_tools():
        return definitions

    @server.call_tool()
    async def call_tool(name, arguments):
        return await astra.call(name, arguments)

    @server.list_prompts()
    async def list_prompts():
        return [PROMPT]

    @server.get_prompt()
    async def get_prompt(name, arguments):
        if name != PROMPT.name:
            raise ValueError(f"Unknown prompt {name}")
        arguments = arguments or {}
        quality = arguments.get("quality") or "studio"
        if quality not in {"draft", "studio", "final"}:
            quality = "studio"
        text = build_prompt(arguments.get("brief", ""), quality, astra.lab is not None)
        return GetPromptResult(
            description=PROMPT.description,
            messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
        )

    return server


async def serve_stdio(config, output, viewer_port=8765):
    from mcp.server.stdio import stdio_server

    astra = AstraServer(config, output, viewer_port=viewer_port)
    server = create_server(astra)
    try:
        if viewer_port is not None:
            with suppress(Exception):
                await astra.start_viewer()
        async with stdio_server() as (read, write):
            await server.run(
                read,
                write,
                InitializationOptions(
                    server_name="astra-blender",
                    server_version=__version__,
                    capabilities=server.get_capabilities(NotificationOptions(), {}),
                    instructions=INSTRUCTIONS,
                ),
            )
    finally:
        await astra.close()
