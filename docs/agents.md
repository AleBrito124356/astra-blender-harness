# Using Astra from an agent

`astra-blender mcp` is a stdio MCP server. Every client below starts the same command; only the configuration format differs. The examples use `uvx` so nothing has to be installed first; with Astra installed in a virtual environment, use that environment's `astra-blender` (or `python -m astra_blender`) as the command instead.

```text
uvx --from git+https://github.com/AleBrito124356/astra-blender-harness astra-blender mcp
```

## Which Blender the agent gets

| Setup | What to do |
| --- | --- |
| Blender open on your desktop | Install the Blender MCP add-on and press its start button (sidebar of the 3D viewport). The default `auto` backend finds it on 127.0.0.1:9876. |
| No desktop Blender (cloud agent, container, CI) | Install the bpy wheel in a matching Python - bpy 5.2 needs Python 3.13, bpy 4.5 needs Python 3.11 - and set `ASTRA_BLENDER` to that Python. Or put a Blender executable on PATH, or set `ASTRA_BLENDER` to it. |
| Both, and you want headless | Add `--backend headless` (or set `ASTRA_BACKEND=headless`). |
| Start from an existing file, headless | Add `--file /path/to/scene.blend`. The file is opened with embedded scripts disabled and never overwritten. |

`blender_status` reports which backend was chosen, the Blender version, and the live viewer's URL (default http://127.0.0.1:8765; another free port when that one is busy, `--no-viewer` to skip it).

## Claude Code

As a plugin (adds the server and the `astra-blender` skill):

```text
/plugin marketplace add AleBrito124356/astra-blender-harness
/plugin install astra-blender@astra-blender
```

Or just the server:

```sh
claude mcp add astra-blender -- uvx --from git+https://github.com/AleBrito124356/astra-blender-harness astra-blender mcp
# headless, with a bpy Python:
claude mcp add astra-blender -e ASTRA_BLENDER=/opt/py313/bin/python -- uvx --from git+https://github.com/AleBrito124356/astra-blender-harness astra-blender mcp --backend headless
```

Claude Code asks before each tool that changes the scene (`run_python`, `frame_camera`, ...). Read-only tools are marked as such.

## Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.astra-blender]
command = "uvx"
args = ["--from", "git+https://github.com/AleBrito124356/astra-blender-harness", "astra-blender", "mcp"]
# env = { ASTRA_BLENDER = "/opt/py313/bin/python" }
```

## Cursor, Claude Desktop, Windsurf, Gemini CLI

These read an `mcpServers` object (`.cursor/mcp.json`, `claude_desktop_config.json`, Gemini CLI's `settings.json`):

```json
{
  "mcpServers": {
    "astra-blender": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/AleBrito124356/astra-blender-harness", "astra-blender", "mcp"],
      "env": {"ASTRA_BLENDER": "/opt/py313/bin/python"}
    }
  }
}
```

Drop `env` when you use a desktop Blender.

## VS Code

`.vscode/mcp.json`:

```json
{
  "servers": {
    "astra-blender": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/AleBrito124356/astra-blender-harness", "astra-blender", "mcp"]
    }
  }
}
```

## A good first request

Give the agent a subject, its real-world size, a mood and a deliverable:

> Model a 30 cm ceramic table lamp with a linen shade on a walnut side table. Warm product-shot lighting, AgX, framed for a 4:5 post. Save a checkpoint before materials, finish with quality_check and tell me what is still weak.

The `build_scene` prompt (in clients that list MCP prompts) packages the same workflow for any brief.

## What the agent sees

`run_python` answers with the script's output, then the audit:

```text
Output:
made Planet

After-edit audit: 2 objects, camera 'Camera'. 3 new, 0 resolved, 0 still open.
New:
- warning outside_frame: Planet - Subject centers outside the render frame. Check the camera before rendering.
- warning cropped_bounds: Planet - Subject bounds cross the frame. Use frame_camera with intended subject names.
- warning no_material: Planet - Geometry has no assigned material.
```

After `frame_camera(objects=["Planet", "Cube"])`:

```text
{'camera': 'Camera', 'subjects': ['Planet', 'Cube'], 'margin': 0.12, 'message': 'Subject bounding boxes fit inside the camera margin. Occlusion still needs review.'}

After-edit audit: 2 objects, camera 'Camera'. 0 new, 2 resolved, 1 still open.
Resolved:
- warning outside_frame: Planet - ...
- warning cropped_bounds: Planet - ...
Still open: no_material
```

(Real output from a headless Blender 5.2.3 session.) A refused script answers with the line and the reason - `Astra Guard rejected this script before it reached Blender - line 1: import of 'os' is not allowed. Allowed modules: ...` - and nothing reaches Blender.

## Session files

Each server process writes to `runs/agent-<timestamp>/` (change with `--output`): `000-start.blend` (taken before the first edit), named checkpoints, `edits.jsonl` (every script with its intent and outcome), `quality.json` (latest audit), `look-NNN.png`, and, after `quality_check`, `inspection.json` and `technical.json`.
