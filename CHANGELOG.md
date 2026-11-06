# Changelog

## 0.4.1 — 2026-11-06

- A turn that gets no answer is now one request, and the per-turn limit is a real deadline. 0.3.2 said a timed-out turn was not retried, and that was not true: `num_retries=0` only reaches LiteLLM's own retry, while the OpenAI client under it repeats a timed-out request twice more with nothing in the trace. On NVIDIA NIM a 300 s limit took 15 minutes, with the studio showing no progress. The limit now covers the whole turn, whatever the layers below decide to do, including the one retry for a transient failure.
- A turn timeout can no longer be read as the run's own deadline.
- 0.3.2's test replaced LiteLLM with a fake and could not see any of this. The new test uses a real socket that never answers and counts the requests that reach it.

## 0.4.0 — 2026-11-02

Astra becomes the guarded Blender layer for any AI agent, not only for its own loop.

### Added

- **MCP server** (`astra-blender mcp`, stdio) for Claude Code, Codex, Cursor, Claude Desktop, VS Code, Gemini CLI or any MCP client. Tools: `blender_status`, `inspect_scene`, `run_python`, `look`, `frame_camera`, `assemble_parts`, `place_on_ground`, `keyframe_object`, `inspect_animation`, `checkpoint`, `restore_checkpoint`, and `quality_check` when Blender Quality Lab is installed. `run_python` answers with the script's output plus an after-edit audit delta: problems the edit introduced, problems it fixed, problems still open. The first edit of a session checkpoints the scene. Read-only and destructive tools carry MCP annotations. A `build_scene` prompt packages the workflow. Session files go to `runs/agent-<timestamp>/`.
- **Blender Quality Lab integration**: with the lab installed (API 1), the MCP server offers `quality_check` and the studio loop offers `astra_quality_check` in every phase, reviewed in review and applied in refine. Both run the lab's inspector inside the live Blender as one of Astra's reviewed scripts and return its gates and "Fix first" list with Python; `inspection.json` and `technical.json` are saved with the run.
- **Live viewer for agent sessions**: the MCP server starts the studio on 127.0.0.1:8765 (or a free port) sharing its Blender connection, so a person watches what the agent builds. `--no-viewer` turns it off.
- **Headless backend**: Astra starts a background Blender - the `bpy` wheel in a matching Python, or a Blender executable - running `headless_worker.py`, which speaks the Blender MCP add-on's socket protocol on 127.0.0.1 behind a per-process token. No GUI: CI, containers and cloud agents can build scenes. The worker gets an allowlisted environment (no API keys), opens `--file` with embedded scripts disabled, renders `look`/screenshot previews through the scene camera (Workbench, or Cycles CPU without EGL) and restores every render setting it touched, and exits when Astra does.
- **Socket backend** (the new default for `serve` and `run`): Astra talks to the Blender MCP add-on directly. No `uvx`, no second MCP server process, one hop fewer for the live viewer, and no dependence on which tool names the upstream server exposes. Large replies are parsed without quadratic re-decoding; a command that times out resets the connection instead of reading its late answer as the next reply.
- **`auto` backend** (default for `mcp`): the socket when the add-on is listening, otherwise headless, decided at each connection.
- **Astra Guard**: static checks on model-written Python before it reaches Blender, on every backend - an import allowlist; no `eval`/`exec`/`open`/`__import__`, dunder attributes, frame internals or computed `getattr` names; no handlers, timers, class registration, text datablocks, linked libraries, script/preferences/extensions operators or auto-run; no scene-file operators. Sensitive namespaces (`bpy`, `bpy.ops`, `bpy.ops.<group>`, `bpy.app`, `bpy.utils`) may only be used directly or via plain aliases, aliases are resolved flow-insensitively, and the most dangerous operator names are refused on any receiver, so hiding a receiver does not hide the operator. `numpy`, `typing` and `string` are not importable. Refusals name the line and reason and happen before the approval prompt. Astra's reviewed scripts are not affected. `--no-guard` disables it.
- **Continuing a headless run** works across Astra restarts: a headless run that stops early keeps `stopped.blend`, and a continued run started in a different Blender process reopens the newest copy of the scene (`stopped`, `scene`, `refine`, `build` or `checkpoint`) before inspecting. A run continued in the same Blender keeps the live scene, as before.
- **Prompt caching for Claude models** in the studio loop: the system prompt and newest message are cache breakpoints, so each turn reads the earlier conversation from cache. Cached tokens are counted in `manifest.json` and usage events. `--no-prompt-cache` turns it off.
- **Claude Code plugin** (`.claude-plugin/`) with the MCP server and an `astra-blender` skill, installable from this repository as a marketplace.
- CLI: `--backend`, `--blender`, `--file`, `--port-blender`, `--no-guard` on every command; `ASTRA_BACKEND` and `ASTRA_BLENDER` environment variables; `python -m astra_blender`; `doctor` reports the backend, the Blender found and its version.
- `headless` extra (`bpy>=4.5`), `examples/` connection files, `docs/agents.md`.
- CI workflow: tests on Linux, Windows and macOS with Python 3.11 and 3.13, the real-Blender tests against bpy 5.2 and 4.5, and the studio's browser checks with a bundle-reproducibility check.

### Fixed

- Blender MCP 2.x replaced `get_viewport_screenshot` with `look`, so discovery failed with "missing required tools" against the add-on users install today. Its `look` now stands in for the old name.
- A Blender that could not be started or reached was reported as a generic disconnect; the studio's connection check now says what to start or install.
- `uvx is not installed` went to stdout, which would corrupt the MCP protocol stream; it goes to stderr.

### Changed

- Default transport is `socket` instead of `stdio` running `uvx blender-mcp==1.9.1`. The upstream MCP server remains available as `--backend stdio` (see `examples/blender-mcp-server.json`).
- The live integration tests no longer need a GUI Blender: they run against a headless Blender whenever `ASTRA_BLENDER` (or bpy, or `blender` on PATH) is available, or against `ASTRA_LIVE_MCP_CONFIG` as before.
- `mcp>=1.30`. Model recommendations updated to current identifiers.

## 0.3.2 — 2026-09-14

- The per-turn model timeout is a setting (default 300 s, was a hard-coded 120 s). A 2.8T reasoning model on NVIDIA NIM spent longer than that thinking before its first token, and the run had no way to wait.
- A timed-out turn is no longer retried (not actually true until 0.4.1: the OpenAI client under LiteLLM kept retrying). LiteLLM's own retry doubled the wait and the provider's bill for a generation that was discarded; transient connection and server errors are still retried once, as documented.
- A timeout says which setting to raise instead of listing five things to check.
- The studio notices when its own files on disk are newer than the running server - what a `git pull` with the server open leaves behind - and says to restart instead of refusing every run as an invalid request.
- A rejected request names the fields it tripped on, by name only; values, including the API key, are still never echoed.

## 0.3.1 — 2026-09-14

- Model identifiers LiteLLM cannot route are refused before the run starts, naming the prefixed form to use. Pasting what a provider's page shows was the most common way a run died on its first model turn, after Blender had already been read and checkpointed.
- Provider failures name their HTTP status and what to do about it: 401/403 the key, 404 the identifier, 410 a retired model, 429 a quota. The provider's own body still stays out of the trace and goes to `error.log`.

## 0.3.0 — 2026-09-11

- Reference photos: up to three PNG, JPEG or WebP images per run, normalized locally (EXIF removed, 1536 px, re-encoded as JPEG), sent to vision models as modeling targets and saved with the run. Carried over when a run is continued.
- Motion per run: detect from the brief, animate, or still scene, with frame count and fps. The deliverable keeps editable parts and keyframes in the .blend.
- Trusted assembly and animation tools for the model: `astra_assemble_parts`, `astra_place_on_ground`, `astra_keyframe_object` and `astra_inspect_animation` (evaluated poses at up to five frames, saved as `animation.json`). Camera fitting accepts frames so the framing covers the whole motion.
- Motion preview: the timeline bakes the animation's world matrices in one read (`POST /api/scene/bake`) and plays them in the browser - play, pause, loop, ½×/2× speed, instant scrubbing, space to toggle - without moving Blender's playhead or asking Blender per frame, and while the model is still running. Deforming meshes keep their current pose and are listed. Stale bakes are detected through a keyframe fingerprint and refreshed. The timeline is visible before there are keyframes, disabled, with the reason.
- Incremental live sync: the probe hashes each evaluated mesh and serializes only what changed since the last read; the viewer keeps unchanged geometry and updates matrices.
- Motion sweep: `astra_inspect_animation` samples bounding boxes at sixteen frames and the harness reports new overlaps, ground breaches and detached parts to the model.
- Honest outcomes: motion words in a brief are a hint, not a gate; only Motion set to Animate requires keyframes, and an unmet requirement ends as `incomplete` after `scene.blend` is saved, continuable like a budget-exhausted run.
- `blender-mcp` fallback: when `uvx` is not installed, the CLI uses the `blender-mcp` installed next to its interpreter. Explicit commands and HTTP transports are untouched.
- Run history selector, maximum response tokens setting, keyless local models, and interrupted runs listed for continuation.

## 0.2.0 — 2026-09-11

- Large interactive live Blender geometry viewer with preserved navigation, selection, framing, wireframe, pause, render-camera projection and mobile layout.
- One serialized MCP connection for human preview and model operations; coalesced snapshots and unchanged-revision caching.
- Numerical scene evidence for every model, including evaluated world bounds, camera coverage, material assignments, containment hints and Blender API capabilities.
- Approved camera-fitting tool with explicit subjects, margins and perspective/orthographic support.
- No screenshot requests or image tool exposure for text-only runs.
- Audit partial edits after Python errors; save quality.json and per-tool conversation checkpoints.
- Recover interrupted native tool histories without blindly replaying operations; surface runs even when no final manifest exists.
- Automatic build budgets reserve finishing turns, provide remaining-budget hints and repair empty model responses.
- Preserve custom gateway IDs in saved setups; allow keyless local models; expose response-token settings.
- Detect an older backend explicitly instead of presenting an unsynchronized empty 3D scene.
- Retain saved setups, OS keyring integration, model discovery, large viewport expansion, gallery and continuation controls from the preceding studio updates.
