# Changelog

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
