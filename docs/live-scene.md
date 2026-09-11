# Live scene and numerical feedback

Astra 0.2 separates the human workspace from model evidence.

- **Humans:** an interactive Three.js view of evaluated Blender meshes, instances, material colors and split normals. Orbit, zoom, fit the scene, select/focus an object, view wireframe and inspect the render camera. Navigation happens locally in the browser.
- **Text models:** world bounds, dimensions, camera projections, material assignments, containment hints and actual Blender API capabilities. No image tool or image payload when Vision feedback is off.
- **Vision models:** the same numerical evidence plus PNG viewport captures. Captures do not replace or move the human's interactive view.

## Synchronization

A single MCP owner serializes reads and edits through one connection. The browser polls about every 1.8 seconds while visible and unpaused; snapshots are coalesced and cached for at least 1.5 seconds. Unchanged revisions omit geometry. Camera navigation is preserved across updates.

Blender runs commands on its main thread. During an edit/render the browser keeps the latest scene and remains navigable; it cannot show intermediate states inside one blocking Python command. Changes appear after Blender processes the next read. This is a live geometry viewer, not an embedded native Blender window or a streamed Cycles viewport.

The viewer reads on opening the page. It executes a fixed, reviewed inspection script through the configured MCP, never model-written code. It does not send geometry to an LLM by itself. It respects upstream safe mode without extending its allowlist.

Preview limits: 250 visible geometry objects/instances, 45,000 vertices and 60,000 triangles. Objects beyond the mesh budget use bounding boxes; a warning identifies truncation. Evaluated geometry includes modifiers available in the current dependency graph. Render-only modifier visibility, particles, volumes, texture graphs, displacement, lighting and compositing may differ from a final render. Preview shading is deliberately simple. Use Images/final renders for actual appearance.

Perspective and orthographic camera preview uses Blender's render frame, lens shift, clipping, pixel aspect and sensor fit. Panoramic projection is unsupported. Orbiting or selecting changes only the browser view, never the Blender scene or camera.

## Tools for the model

**astra_inspect_scene** is read-only and available in every phase. It reports current Blender version, shader socket names, Eevee RNA properties and object-level measurements. The harness adds an audit after attempted edits, including Python errors that may have changed part of the scene.

**astra_frame_camera** takes exact subject names and an optional margin (default 12%). It fits a perspective or orthographic camera around evaluated subject bounds, including instances, while retaining the current viewing side. Exclude broad floors/backgrounds. The operation follows normal mutation approval and only adjusts the camera; constrained camera rigs are rejected. It does not resolve occlusion or move geometry.

Checks distinguish missing cameras, cropped bounds, off-frame centers, missing material assignments and possible unintended containment. Containment is a bounding-box hint, not a mesh-intersection verdict. Intentional nesting is common. Numerical checks do not establish aesthetic quality.

Each run saves the latest report as quality.json. Successful export and a completed run do not mean all quality warnings were resolved.

## Recovery and budgets

Conversation state is atomically replaced before an action batch and after each tool result. After a crash, unmatched native tool calls receive an explicit unknown-outcome result; Astra re-inspects and never automatically replays uncertain edits. Saved state contains no images or configured API key. Keep the intended scene open: resuming a conversation does not restore a .blend file automatically.

Automatic build budgeting can use the remaining turns, reserving a read-only review turn and action/assessment turns for later editing phases. The model receives remaining phase/token hints. Explicit phase limits remain respected. Empty native responses trigger repair, not phase completion.

## Frontend development

The browser bundle is committed and included in the wheel; end users need no Node.js or CDN access.

```sh
npm ci
npm run build
# Start astra-blender serve --port 8766 in another terminal.
npx playwright install chromium
npm run test:ui
```

Set ASTRA_TEST_URL for a different local port. Set ASTRA_BROWSER_CHANNEL=msedge to test with installed Edge. Browser tests intercept every API request and never call a provider, read saved keys or edit Blender. Three.js's license is included in the packaged static/THIRD_PARTY_NOTICES.txt.
