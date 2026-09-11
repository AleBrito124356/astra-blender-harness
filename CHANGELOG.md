# Changelog

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
