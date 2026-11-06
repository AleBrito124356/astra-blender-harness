# Validation record

Validated on Windows, Python 3.14 and Blender 4.5.9 LTS on 2026-09-09.

- Unit/integration suite exercises phase completion, budgets, read-only phases, approval denial/cancellation, JSON repair, secret redaction, save-file verification, local Host/Origin/auth checks and provider argument adaptation.
- A real stdio MCP fixture verifies the SDK handshake, tool discovery, schemas and image transport.
- **Live Blender integration passed** using the official `blender-mcp==1.9.1` package and its bundled add-on in a disposable Blender 4.5.9 GUI process. A scripted provider drove the actual harness: inspect scene, capture viewport, save checkpoint, create geometry, inspect again and save the final `.blend`. Files were verified on disk. This was not a model API call.
- Edge UI checks passed at 1440 × 1080 and 390 × 844: scene preset, provider switching, complete offline demo, three preview images, manifest download, no horizontal mobile overflow and no JavaScript exceptions.
- The companion lab generated three real 960 × 720 Cycles renders and reopened all three scenes for inspection.
- The supplied CI template targets Python 3.11/3.13 on Windows and Ubuntu; it is not activated in GitHub Actions. The live Blender test is opt-in.

## 0.2 validation — 2026-09-10

- Python unit/protocol tests cover numerical diagnostics, serialized MCP ownership, cancellation without mutation replay, reconnect, coalesced scene revisions, orphaned tool-history repair, missing-manifest recovery, text-only screenshot exclusion, empty response repair and automatic build budgets.
- Both opt-in live tests passed in a disposable Blender 5.2.1 GUI through official MCP 1.9.1 safe mode: create a torus, fit its camera, assert all eight bounding-box corners are in frame, verify updated live geometry/normals and scene copies. Vision mode captures PNGs; text-only mode captures none. Scripted provider only, no model API call.
- A saved scene with cropped subjects was opened in a separate background Blender process: the camera fitter brought all subject bounds into frame. The original was unchanged. Projection was compared with Blender's own helper for perspective/orthographic cameras, shifted lenses, portrait output and non-square pixels.
- Headless Edge checks exercise a restored custom gateway ID, live revision changes, orbit pixels, selection, pause, expansion, local keyless submission and 390px mobile layout, with every API intercepted. A separate browser check displayed 28 real objects from live Blender with no JavaScript errors.
- No paid model or comparative aesthetic benchmark was run for 0.2. Existing text-only model traces motivated the numerical checks; those checks improve available evidence, not guarantee artistry.

## Repeat the live check

Install the official add-on, start its server in a **disposable** Blender scene, install `blender-mcp`, and create a local MCP configuration with its executable. Set `ASTRA_LIVE_MCP_CONFIG` to that configuration's absolute path, then:

```sh
python -m pytest tests/test_live_blender.py -q
```

This creates test tori, adjusts the camera and saves scene copies. It does not require an LLM API key.

## Limits of the evidence

No paid OpenAI, Anthropic, Google or OpenRouter request was made. Provider routing is tested with mocked SDK responses; real account/model permissions and provider-specific behavior still need testing with the user's own key. No aesthetic model benchmark or cross-platform Blender test is claimed. The demo image is an illustration. The live smoke test proves integration and artifact creation, not artistic quality.

On 2026-09-11, final checks passed: 71 Python tests (two opt-in live tests skipped in the ordinary suite), frontend browser checks including the old-backend regression, Ruff and package build. The two opt-in Blender 5.2 tests had passed separately. The updated local server returned all 28 objects from the user scene, and the browser object list matched that snapshot exactly. No paid model calls were made.

## 0.3.0 motion preview and incremental sync (2026-09-11)

Exercised against a live Blender 5.2.1 LTS session with 54 objects (a house, a car assembly and a ground plane) through the packaged `blender-mcp` 1.9.1 with upstream safe mode on:

- Full scene probe with geometry: 269 ms, 349 KB. Incremental probe with every hash known: 75-91 ms, 68 KB, all 54 meshes reported unchanged and merged from the previous snapshot. Through the studio, a frame seek with a fresh probe went from 229-257 ms to 142-163 ms.
- Motion bake of the whole frame range: 66 ms. `astra_inspect_animation` with the sixteen-frame bounding-box sweep: 146 ms.
- Safe mode rejects `hashlib`; digests use the `hash()` builtin. Browser tests were run with an installed Edge (`ASTRA_BROWSER_CHANNEL=msedge`) because the Playwright Chromium on the machine did not match the package version.
- The four still-scene briefs that were auto-classified as animation ("motion blur", "walking path", "luz anima la escena", "rotating-door mechanism") now complete as still scenes; a Motion: Animate run without keyframes ends as `incomplete` with `scene.blend` saved.

## 0.4.1 turn deadline (2026-11-06)

Windows 11, Python 3.14, LiteLLM 1.100.0, real Blender 5.2.1 LTS.

- Against a local socket that accepts a request and never answers, a 3 s limit produced 3 requests in 11 s before this change and 1 request in 3 s after it. `num_retries=0` and `max_retries=0` both left it at 3.
- The failure was seen on a real run: `deepseek-ai/deepseek-v4.1-flash` on NVIDIA NIM gave no answer, and the connection to NVIDIA was reopened at 300 s and 600 s with no event in the trace.

## 0.4.0 agent-native release (2026-11-02)

Exercised on Linux (x86-64 container, no GPU, Mesa software EGL), Python 3.13, against real Blender from the PyPI `bpy` wheels **5.2.3 LTS** (Python 3.13) and **4.5.15 LTS** (Python 3.11), headless:

- The live integration tests that used to need a disposable GUI Blender - a full engine run creating, framing and saving a torus with vision on and off, and the reference-photo assembly animation with motion framing - pass against both Blender versions through the new headless backend. So do the headless worker tests (round trip, partial output on errors, preview PNG, verified copy, token refusal, exit when Astra's pipe closes) and the MCP server test, in which a real MCP client over stdio drives `astra-blender mcp`: status, `run_python` with the audit, a Guard refusal, `frame_camera`, focused `inspect_scene`, checkpoint, deletion, restore, `look` and the `build_scene` prompt. Eight tests need a real Blender and pass on both versions - including continuing a failed headless run in a second Blender process, which reopens the scene copy the first one kept; with Blender available the whole suite is 183 tests (175 pass and 8 skip without one).
- Timings for a scene of 61 meshes (60 UV spheres plus the default cube), Blender 5.2.3 / 4.5.15: worker start 0.73 / 0.62 s; scene probe with full geometry 0.98 / 0.91 s (6.5 MB); incremental probe with every hash known 83 / 25 ms (75 KB); after-edit audit 35 / 15 ms; 768 px Workbench preview through the camera 0.64 / 0.56 s; verified `.blend` copy 11 / 8 ms.
- Real agent-loop output, captured from a headless 5.2.3 session through the MCP server: adding a sphere outside the frame reported `outside_frame`, `cropped_bounds` and `no_material` as new; `frame_camera` then reported the two framing problems resolved and `no_material` still open (quoted in [agents.md](agents.md)).
- The socket backend was exercised against a replica of the Blender MCP add-on's protocol (unframed JSON objects, replies split across writes, 3 MB replies with unbalanced braces inside strings, error payloads with tracebacks, a reply arriving after its timeout, screenshots written to a file). It was **not** run against a GUI Blender in this release; the add-on refuses to start in background mode, and no GUI Blender was available.
- Blender MCP 2.x compatibility was exercised against a stdio fixture with 2.x's tool shape (`look`, no `get_viewport_screenshot`), not against the upstream server itself.
- Astra Guard: 69 tests - seven kinds of ordinary scene code accepted (primitives and modifiers, node materials with literal-keyed loops, bmesh with `array` and `foreach_get`, render settings including Cycles device preferences, keyframes and drivers, imports of models, operator aliases and literal `getattr`/`hasattr`), 56 hostile constructs refused, and Astra's own reviewed scripts accepted. An independent review found that the first version could be bypassed by hiding the receiver of a refused operator (a walrus, a list, a lambda, a loop-variable `getattr`); those forms and their relatives (function arguments, defaults, return values, containers, branch aliases, alias chains, conditionals) are now refused and covered by tests, and the reviewer's probe script reports every payload blocked.
- Prompt caching: a fake Anthropic endpoint captured the request LiteLLM actually sends; the system prompt and the newest message carry `cache_control`, nothing else does, and `prompt_cache=false` removes both.
- Studio browser checks (both suites) pass with Playwright's Chromium against a running server; the committed viewer bundle rebuilds byte-identical with esbuild 0.25.12.
- No paid model API call was made for this release.
