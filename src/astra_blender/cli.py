import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from .bridge import connect, discover
from .config import MCPConfig, RunConfig
from .engine import Run, execute, result_parts


def fallback_transport(config):
    """Use the interpreter's own blender-mcp when uvx is not installed.

    The default transport spawns `uvx blender-mcp==1.9.1`. On a machine without
    uv that fails at spawn time, and the studio can only report it as "Cannot
    reach Blender" - even though blender-mcp is already installed as a
    dependency right next to this interpreter. Explicit commands are respected.
    """
    if config.transport != "stdio" or config.command != "uvx" or shutil.which("uvx"):
        return config
    scripts = Path(sys.executable).parent
    for name in ("blender-mcp.exe", "blender-mcp"):
        candidate = scripts / name
        if candidate.is_file():
            print(f"uvx is not installed; using {candidate} instead")
            return config.model_copy(update={"command": str(candidate), "args": []})
    return config


def main():
    parser = argparse.ArgumentParser(description="Astra — a local Blender MCP studio")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Open the local studio")
    serve.add_argument("--port", type=int, default=8765)
    doctor = sub.add_parser("doctor", help="Check the actual Blender MCP connection")
    run = sub.add_parser("run", help="Create a scene from the terminal")
    run.add_argument("prompt")
    run.add_argument("--model", default="openai/gpt-6-astra")
    run.add_argument("--api-base")
    run.add_argument("--tool-mode", choices=["native", "json"], default="native")
    run.add_argument("--no-vision", action="store_true")
    run.add_argument("--auto-approve", action="store_true")
    run.add_argument("--quality", choices=["draft", "studio", "final"], default="studio")
    run.add_argument("--max-steps", type=int, default=24)
    run.add_argument(
        "--reference",
        type=Path,
        action="append",
        default=[],
        help="Reference photo (repeat up to 3; requires vision)",
    )
    run.add_argument("--animation", choices=["auto", "on", "off"], default="auto")
    run.add_argument("--frames", type=int, default=120)
    run.add_argument("--fps", type=int, default=24)
    for command in (serve, doctor, run):
        command.add_argument("--config", type=Path)
        command.add_argument("--output", type=Path, default=Path("runs"))
    args = parser.parse_args()
    config = fallback_transport(
        MCPConfig.model_validate_json(args.config.read_text(encoding="utf-8")) if args.config else MCPConfig()
    )
    if args.command == "serve":
        import uvicorn

        from .server import create_app

        print(f"Astra Blender Studio: http://127.0.0.1:{args.port}")
        uvicorn.run(create_app(config, args.output), host="127.0.0.1", port=args.port, log_level="warning")
    elif args.command == "doctor":
        asyncio.run(_doctor(config))
    else:
        settings = RunConfig(
            prompt=args.prompt,
            model=args.model,
            api_base=args.api_base,
            api_key=os.environ.get("ASTRA_API_KEY", ""),
            vision=not args.no_vision,
            tool_mode=args.tool_mode,
            auto_approve=args.auto_approve,
            quality=args.quality,
            max_steps=args.max_steps,
            animation=args.animation,
            animation_frames=args.frames,
            animation_fps=args.fps,
        )
        result = asyncio.run(_run(settings, config, args.output, args.reference))
        raise SystemExit(0 if result == "completed" else 1)


async def _doctor(config):
    async with asyncio.timeout(30):
        async with connect(config) as client:
            found = await discover(client, config.allowed_tools)
            arguments = (
                {"user_prompt": "Check Blender connection"}
                if "user_prompt" in found["get_scene_info"].inputSchema.get("properties", {})
                else {}
            )
            scene = await client.call_tool("get_scene_info", arguments)
            if result_parts(scene)[0].startswith("TOOL ERROR:"):
                raise RuntimeError("MCP is up but Blender returned an error")
            print("Blender connected. Tools: " + ", ".join(found))


async def _run(settings, config, output, reference_paths=()):
    from . import references

    if len(reference_paths) > 3:
        raise ValueError("Use at most three reference images")
    run = Run(settings, output)
    references.attach(run, reference_paths)
    original_emit = run.emit

    def emit(kind, **data):
        original_emit(kind, **data)
        if kind in {"phase", "assistant", "failed", "completed", "budget_exhausted", "approval"}:
            print(json.dumps(run.events[-1], ensure_ascii=False))

    run.emit = emit

    async def approve(name, arguments):
        from .engine import READ_ONLY

        if settings.auto_approve or name in READ_ONLY:
            return True
        print(run.clean(json.dumps({"tool": name, "arguments": arguments}, indent=2)))
        answer = await asyncio.to_thread(input, "Execute this Blender operation? [y/N] ")
        return answer.lower().strip() == "y"

    run.approve = approve
    await execute(run, config)
    print(f"{run.status}: {run.directory}")
    return run.status
