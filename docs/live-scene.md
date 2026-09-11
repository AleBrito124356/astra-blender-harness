# Live scene, motion preview and numerical feedback

Astra separates the human workspace from model evidence.

- **Humans:** an interactive Three.js view of evaluated Blender meshes, instances, material colors and split normals. Orbit, zoom, fit the scene, select/focus an object, view wireframe, inspect the render camera, and play the animation. Navigation and motion preview happen locally in the browser.
- **Text models:** world bounds, dimensions, camera projections, material assignments, containment hints, motion evidence and actual Blender API capabilities. No image tool or image payload when Vision feedback is off.
- **Vision models:** the same numerical evidence plus PNG viewport captures. Captures do not replace or move the human's interactive view.

## Synchronization

A single MCP owner serializes reads and edits through one connection. The browser polls about every 1.8 seconds while visible, unpaused and not previewing motion; snapshots are coalesced and cached for at least 1.5 seconds. Unchanged revisions omit geometry.

Reads are incremental. Every visible instance carries a stable key (the object name, then `name#2`, `name#3` for further instances) and each mesh a hash of its evaluated vertex coordinates, counts and material summary. The server sends Blender the hashes it already holds and Blender serializes only meshes whose hash differs, returning a matrix and hash for the rest; the server merges those from the previous snapshot, so everything downstream still sees a complete scene. The viewer keeps any node whose hash is unchanged and updates only its matrix. Curves, text and metaballs have no fast signature and are serialized every time.

Blender runs commands on its main thread. During an edit/render the browser keeps the latest scene and remains navigable; it cannot show intermediate states inside one blocking Python command. Changes appear after Blender processes the next read. This is a live geometry viewer, not an embedded native Blender window or a streamed Cycles viewport.

The viewer reads on opening the page. It executes fixed, reviewed inspection scripts through the configured MCP, never model-written code. It does not send geometry to an LLM by itself. It respects upstream safe mode without extending its allowlist.

Preview limits: 250 visible geometry objects/instances, 45,000 vertices and 60,000 triangles. Objects beyond the mesh budget use bounding boxes; a warning identifies truncation. Evaluated geometry includes modifiers available in the current dependency graph. Render-only modifier visibility, particles, volumes, texture graphs, displacement, lighting and compositing may differ from a final render. Preview shading is deliberately simple. Use Images/final renders for actual appearance.

Perspective and orthographic camera preview uses Blender's render frame, lens shift, clipping, pixel aspect and sensor fit. Panoramic projection is unsupported. Orbiting, selecting, scrubbing or playing changes only the browser view, never the Blender scene, camera or playhead.

## Motion preview

Rigid motion - parts of an assembly separating, wheels turning, a root translating - changes an object's matrix, not its mesh. So the timeline bakes the whole animation in one read: `POST /api/scene/bake` runs a trusted script that samples the world matrix of every moving instance (objects with an action, NLA strips, drivers, constraints or armature parenting, and their descendants) across the scene frame range, coarsening the step automatically beyond 60,000 samples and restoring the playhead itself. The browser decomposes the tracks once and plays them at display rate with interpolation between samples.

Controls: Play/Pause (also the space bar while the view has focus), Loop, ½×/1×/2× speed, a frame slider that scrubs instantly, and **Back to live**, which restores the rest pose and resumes the sync poll. While a preview is engaged the poll is paused so a snapshot cannot snap the parts back. The timeline is visible whenever the backend supports it; until the scene has keyframes its controls are disabled and the note says so.

The probe includes a fingerprint of the animated objects' keyframes. A bake whose fingerprint no longer matches is marked stale and refreshed automatically when nothing is playing; **Rebake** does it on demand. Meshes whose shape itself deforms - armature, shape keys, cloth, soft body, ocean, wave, lattice, geometry nodes and similar modifiers - follow their matrix but keep their current-frame shape; the note lists how many. The bake is not gated on the run lock: it goes through the same serialized hub as the model's operations, so a person can watch the motion while the model keeps working.

`POST /api/scene/frame` still moves Blender's playhead to a frame and refreshes the scene; the interface no longer uses it, and it is refused while a run is active.

## Tools for the model

**astra_inspect_scene** is read-only and available in every phase. It reports current Blender version, shader socket names, Eevee RNA properties and object-level measurements. The harness adds an audit after attempted edits, including Python errors that may have changed part of the scene.

**astra_frame_camera** takes exact subject names and an optional margin (default 12%). It fits a perspective or orthographic camera around evaluated subject bounds, including instances, while retaining the current viewing side. Pass up to five frames to fit the combined motion bounds. Exclude broad floors/backgrounds. The operation follows normal mutation approval and only adjusts the camera; constrained or animated cameras are rejected. It does not resolve occlusion or move geometry.

**astra_assemble_parts** parents distinct parts to an Empty root while preserving their world transforms, and records the chosen anchor and allowed gap on each part. **astra_place_on_ground** translates a whole unanimated assembly so its lowest evaluated point rests on a separate horizontal ground mesh. **astra_keyframe_object** creates location, XYZ Euler rotation and scale keys on an unanimated, unconstrained object or root; values are local to the parent and rotations are radians. Existing actions and constraints are rejected rather than silently overwritten.

**astra_inspect_animation** is read-only. It lists actions and their changing channels, samples evaluated poses at up to five frames (default start, middle, end) through the scene probe, and sweeps bounding boxes at sixteen evenly spaced frames. The harness summarizes the sweep into pairs that start overlapping after the first sampled frame, objects that dip below a broad flat floor, and assembled parts whose gap to their anchor exceeds the allowed maximum; raw boxes never reach the model. Box overlap is a hint, not a mesh-level verdict, and nothing between two samples is observed. Each run saves the latest report as `animation.json`.

Checks distinguish missing cameras, cropped bounds, off-frame centers, missing material assignments and possible unintended containment. Containment is a bounding-box hint, not a mesh-intersection verdict. Intentional nesting is common. Numerical checks do not establish aesthetic quality.

Each run saves the latest scene report as `quality.json`. Successful export and a completed run do not mean all quality warnings were resolved.

## Motion as a deliverable

Motion set to **Animate** makes keyframes a requirement: after the final audit and `scene.blend` save, a run without an action whose values change ends as `incomplete` and can be continued. **Detect from brief** only hints - the model is told the brief may describe motion and what to do if it does; a still scene that mentions motion blur or a walking path is built as a still scene. Motion evidence is collected after the build and refine phases whenever motion was requested or hinted.

## Recovery and budgets

Conversation state is atomically replaced before an action batch and after each tool result. After a crash, unmatched native tool calls receive an explicit unknown-outcome result; Astra re-inspects and never automatically replays uncertain edits. Saved state contains no images or configured API key. Keep the intended scene open: resuming a conversation does not restore a .blend file automatically.

Automatic build budgeting can use the remaining turns, reserving a read-only review turn and action/assessment turns for later editing phases. The model receives remaining phase/token hints. Explicit phase limits remain respected. Empty native responses trigger repair, not phase completion.

## Frontend development

The browser bundle is committed and included in the wheel; end users need no Node.js or CDN access.

```sh
npm ci
npm run build
# Start astra-blender serve in another terminal (port 8765 by default).
npx playwright install chromium
npm run test:ui
```

Set `ASTRA_TEST_URL` to the server's origin when it is not `http://127.0.0.1:8765`. Set `ASTRA_BROWSER_CHANNEL=msedge` to test with an installed Edge instead of downloading Chromium. Browser tests intercept every API request and never call a provider, read saved keys or edit Blender; the motion test drives baked playback against a mocked bake and asserts that the playhead endpoint is never called. Three.js's license is included in the packaged static/THIRD_PARTY_NOTICES.txt.
