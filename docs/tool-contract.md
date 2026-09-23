# Tool contract (Astra 0.4)

Every trusted tool the model can call is one `ToolSpec` in the registry
(`src/astra_blender/registry.py`). The engine builds its tool pool, its
read-only set, its dispatch and its evidence from the registry, so adding a
tool never means editing the engine or a shared list.

This page is the reference for workstreams adding tools. `tools/contract.json`
pins the planned 0.4 tool names and arguments; `docs/events.md` pins the run
events.

## Where a tool lives

`src/astra_blender/tools/__init__.py` lists five modules, one per workstream.
The list is complete for 0.4: nobody edits it.

| Module | Owner | Holds |
|---|---|---|
| `tools/sight.py` | WS1 | perception: `astra_scene_facts`, `astra_look_check`, `astra_motion_facts`, `astra_plan_view`, `astra_inspect_scene` (legacy) |
| `tools/toolkit.py` | WS2 | make and shoot: build, material, arrange, transform, place, repair, cleanup, light rig, camera shot, render setup, exposure, render, video; `astra_frame_camera` (legacy) |
| `tools/motion.py` | WS3 | `astra_animate`, `astra_explode`, `astra_split_parts`, `astra_assemble_parts`, `astra_place_on_ground`, `astra_keyframe_object`, `astra_inspect_animation` |
| `tools/judge.py` | WS4 | harness tools `astra_set_spec`, `astra_check` |
| `tools/guard.py` | WS5 | `astra_api_lookup` |

Each module exports:

- `TOOLS`: a list of `ToolSpec`.
- `PROMPT_CARDS`: `{tier: text}` with what a model must know to use these
  tools. `registry.cards(tier, names)` joins the cards of the modules whose
  tools are offered. A missing tier falls back to a smaller tier's card,
  never a bigger one.
- `RECIPES`: a list of `Recipe(id, keywords, steps, defaults, min_tier, title)`.
- `PORTED_0_3_2` (foundation only): names still exactly as 0.3.2 shipped them.
  `tests/test_registry.py` pins their definitions and generated code while
  they are listed. Remove a name from your own module when you deliberately
  upgrade that tool.

`registry.load()` imports the modules once. A duplicate name, a missing
script file, a harness tool without a handler and similar structural
mistakes raise `RegistryError` at import, not mid-run.

## ToolSpec

| Field | Meaning |
|---|---|
| `name` | `astra_` + lower_snake_case. |
| `description` | What the model reads. 60 words or fewer, with units (m, degrees, frames). |
| `schema` | JSON Schema of the model's arguments: `type: object`, `additionalProperties: false`, never `user_prompt`, never an injected name. |
| `schema_small` | Optional reduced schema for the small tier (`registry.schema_for(name, "small")`, `spec.as_tool("small")`). The foundation engine still offers `schema` to every model; WS6 switches by tier. |
| `kind` | `"blender"` (a script runs in Blender) or `"harness"` (Python on the host). |
| `scripts`, `entry`, `common` | Blender tools: files in `blender_scripts/`, the function the trailer calls, and whether `common.py` is prepended (default `True`; `False` only keeps a 0.3.2 script byte-identical). |
| `handler` | Harness tools: `handler(args, ctx) -> PostResult`, plain or async. |
| `readonly` | Never changes the scene: no approval, allowed in plan and review, no after-edit evidence. |
| `destructive` | Always asks for approval, even with trusted-tool auto-approval; autofix never applies it. The foundation engine only records the flag (`registry.find(name).destructive`); WS6's approval policy enforces it. |
| `expensive` | Counted against a per-run budget (WS6). |
| `stages` | Subset of `plan, layout, look, motion, review, refine, finalize`. |
| `tiers` | Subset of `small, medium, large`. |
| `vision` | `None`: offered either way. `True`: vision models only. `False`: text-only models only. |
| `priority` | Lower sorts first when `registry.offered(..., limit=n)` cuts a menu; ties keep module order. |
| `injected`, `inject` | Harness-only argument names, and an optional `inject(args, ctx) -> dict` hook (see below). |
| `chunk_key`, `max_seconds` | Chunked tools (see below); `max_seconds` (default 60) is also injected as `budget_s`. |
| `post` | `post(data, ctx) -> PostResult`, plain or async. Without it, the model reads the raw `Code executed successfully: ASTRA_SCENE_JSON:{...}` text. |
| `summarize` | `summarize(args) -> str`: one plain sentence for the approval card (`approval.summary`). |
| `examples` | `{"typical": {...}, "max": {...}}`, optional `"hostile"`; each must validate against `schema`. |
| `hidden` | Accepted when called (old transcripts, resumed runs), never offered. |

Registry functions: `load`, `get`, `find`, `all_specs`, `names(readonly=None)`,
`hidden_names`, `read_only_names`, `offered(stage, tier, vision, limit)`,
`schema_for(name, tier)`, `cards(tier, names)`, `recipes(tier)`,
`render_recipe(recipe, bindings)`, `check_args(name, args, tier)`,
`contract()`, `contract_problems(spec)`, `markdown()`.

## Blender tools: how a script is built

`scripts.build(spec, args, injected)` concatenates, with newlines:

1. `common.py` (unless `common=False`);
2. `measure.py`, when the spec lists it;
3. the spec's other scripts, in order, without duplicates;
4. the trailer: `import json` and
   `print('ASTRA_SCENE_JSON:' + json.dumps(<entry>(**<repr(args | injected)>), allow_nan=False))`.

Arguments go in with `repr()`, so quotes, backslashes, emoji or
`__import__('os')` in an object name stay a string literal. Non-finite
numbers are refused with `TOOL ERROR: numbers must be finite`: `json.loads`
accepts `NaN` and jsonschema bounds let it through, but the bare name `nan`
fails safe mode. The engine checks this before asking for approval.

The entry returns a JSON-serialisable dict. New payloads carry `"v": 1`;
`scripts.parse` checks any `v` or `schema_version` field equals 1.

Everything sent to Blender must pass upstream safe mode
(`blender_mcp.safe_mode.is_safe(code)[0]`; it returns a `(bool, reason)`
tuple, so never test its truthiness). Allowed imports: `bpy`, `bmesh`,
`mathutils` and the stdlib allowlist (`array`, `collections`, `json`, `math`,
`random`, `re`, `statistics`, `uuid`...). Not `time`, `os`, `hashlib`,
`difflib`. No `open`, `eval`, `exec`, lambda, class, computed `getattr`,
`dir()`, generators, handlers, timers or drivers. A function passed as a
parameter cannot be called: dispatch on strings. `tests/test_tool_safety.py`
builds every registered Blender tool at its typical, max and six hostile
argument sets and requires `is_safe` plus margins under the upstream limits:
AST depth 20 (limit 24), 8,000 nodes (limit 20,000), 60 KB (limit 200 KB).

Blender 5.2 facts every script relies on: layered actions
(`action.fcurves` does not exist; use `astra_fcurves`), engine id
`BLENDER_EEVEE`, `image_settings.media_type` before `file_format`, Principled
sockets `Transmission Weight` / `Specular IOR Level` / `Emission Color`, AgX
by default, and one call well under 300 s. `matrix_world` is stale after an
edit until `astra_update()`.

### common.py helpers (every tool with `common=True`)

| Helper | Returns |
|---|---|
| `astra_clock()` | seconds on the uuid1 clock, the only clock safe mode allows |
| `astra_budget_left(t0, budget_s)` | seconds left of a budget started at `t0` |
| `astra_update()` | refreshes `matrix_world` and evaluated data |
| `astra_require(names)` | the objects, or `ValueError: Unknown objects: A, B` naming every missing one; call before changing anything, since Blender does not roll back a failed script |
| `astra_is_astra(obj)` | True when tagged `astra_kind` or `astra_role` |
| `astra_world_box(objs)` | evaluated world AABB `(lo, hi)` of objects and their geometry children |
| `astra_fcurves(idblock, ensure=False)` | the channelbag F-curves of an ID's layered action; `ensure=True` creates animation data, action, slot, layer, strip and channelbag |
| `astra_geometry(depsgraph, include_hidden=False)` | one record per render-visible geometry: curve, text, metaball and surface objects counted once, collection and Geometry Nodes instances aggregated per instancer (`kind`, `instances`, `sources`, `lo`, `hi`, `matrix`, `evaluated`) |
| `astra_render_snapshot(scene)` / `astra_render_restore(scene, snapshot=None)` | render and colour settings; the snapshot is also stored in `scene['astra_restore']`, so a later call recovers after a crash |
| `astra_hex_to_linear('#rrggbb')`, `astra_linear_to_hex(rgb)` | sRGB hex and linear colour conversions |

### measure.py helpers (list `measure.py` in `scripts`)

Ported from the prototypes verified on Blender 5.2.1. WS1 owns the file and
may add helpers but keeps these signatures; WS2 and WS3 only call them.

| Helper | Returns |
|---|---|
| `astra_camera_matrix(scene, camera, depsgraph=None)` | projection x view matrix, as the renderer frames it |
| `astra_project_uv(matrix, point)` | `(u, v, behind)`; `v` runs up; matches `world_to_camera_view` to 4 decimals |
| `astra_camera_frame(scene, camera)`, `astra_camera_ray(frame, u, v, clip_start)` | the frame corners and the ray through a frame point (`v` from the top) |
| `astra_ray_grid(w, h, layers=1, budget_s=None)` | depth-peeled camera grid: `rows`, `legend`, per-object `first`/`any` cells, `by` (occluders), boxes, depths, first `hits`, `void`, `rays`, `truncated` |
| `astra_meter(subject_objs, samples=128)` | blind light meter: `irradiance`, `ev` (0 = mid grey, view exposure included), `key_fill`, `ambient_share`, `lights [{name, e, shadowed?, shadowed_by?}]` |
| `astra_lights(scene)`, `astra_world_ambient(scene)`, `astra_light_irradiance(light, point)` | the calibrated terms: SUN S/pi; POINT and SPOT P/(4 pi^2 d^2) with the cone; AREA P/(pi^2 d^2) x facing |
| `astra_world_bvh(obj, depsgraph=None)` | a world-space `BVHTree.FromPolygons` (FromObject is object-local and gave false overlaps) |
| `astra_support_ray(obj, candidates, depsgraph=None)` | `{on, gap_m}`: what the object rests on, negative gap when sunk |
| `astra_object_hit(obj, depsgraph, start, direction)` | where a world ray hits one object, ignoring everything else |

`projection.py`'s `astra_project(scene, camera, point)` remains the per-point
API used by the 0.3.2 probe. The measure variant is named `astra_project_uv`
so the two never shadow each other when both files are concatenated.

## Harness tools

A harness tool runs on the host. It never touches Blender except through
`ctx.call(name, args)`, which runs a read-only registry tool internally (no
approval, no model turn, `tool_call.internal = true`) and returns a
`ToolOutcome`. Anything else raises `ToolFailure`. Harness tools are usually
`readonly=True`, so they are offered in plan and review and never ask.

## PostResult, PostContext, ToolOutcome

`PostResult(text, files={}, events=(), snapshot=None)`:

- `text` is what the model reads. A text starting with `TOOL ERROR:` is
  reported as an error.
- `files`: `{plain file name: str | bytes}` written into the run directory.
  Text is redacted through `Run.clean` first; a name with a path is refused.
- `events`: `((kind, payload), ...)`, emitted in order before `tool_result`.
  Use the payloads pinned in `docs/events.md`.
- `snapshot`: kept as `Run.tool_state["snapshots"][tool]`; WS6 diffs against it.

`PostContext(tier, run_dir, state, clean, emit, call, config, tool, raw)`:
`state` is one dict for the whole run, shared by all tools; `raw` is the raw
result text of a Blender tool. The foundation engine passes `tier="large"`
until WS6 derives the tier from the model profile.

`ToolOutcome(name, text, is_error, executed, data, snapshot, images)`:
`executed` is False when nothing reached Blender (unknown tool, read-only
violation, invalid or non-finite arguments, denial, safe-mode rejection);
`data` is the parsed marker JSON.

Raise `errors.ToolFailure(text)` from a post hook, handler or inject hook to
give the model a `TOOL ERROR:` it can act on. Any other exception is a bug
and fails the run loudly.

## Injected arguments

Names in `injected` are filled by the harness after validation and never
appear in the schema:

| Name | Default value |
|---|---|
| `output_dir` | the run directory, POSIX style |
| `budget_s` | `spec.max_seconds` |
| `start_from` | `None`, then the previous chunk's `chunk_key` value |
| `known` | `Run.tool_state["known"][tool]`, else `{}` |
| `save_to` | `<run_dir>/<name without astra_>-NNN.png`, counting per tool |

Any other name needs `inject(args, ctx)`, which may also override the
defaults (for example `look-NNN.png`). A missing value is a `ToolFailure`.

## Chunked tools

With `chunk_key` set (e.g. `"next_frame"`, and `start_from` in `injected`),
the engine re-runs the script with `start_from = data[chunk_key]` until that
value is `null`, up to 200 chunks, all inside one model call and one
approval. `post` then receives the last chunk's dict plus `chunks`, the list
of every chunk's dict. Each chunk should stop on `astra_budget_left` well
before `budget_s`.

## What the engine does with a call

1. Unknown tool, screenshot without vision, or a non-read-only tool in a
   read-only phase: refused.
2. `user_prompt` and `max_size` injection for upstream tools, then
   `jsonschema.validate` and the non-finite check.
3. Approval (`Run.approve`; read-only tools skip it; the event carries
   `summary` when the spec has `summarize`).
4. `tool_call` event.
5. Registry tool: harness handler, or build + `execute_blender_code` +
   `scripts.classify` + `scripts.parse` + post (looping chunks). Upstream
   tool: sent as is.
6. Post files and events, snapshot, then the `tool_result` event.

`scripts.classify` flags `Error...`/`failed...` texts, a safe-mode rejection
(which upstream returns as an ordinary string) and the guard's
`ASTRA_SCRIPT_ERROR line N`. The after-edit audit follows a turn only when a
non-read-only call actually executed. `engine.READ_ONLY` is
`registry.UPSTREAM_READ_ONLY | registry.names(readonly=True)`.

## Interfaces other workstreams code against

These modules exist with working placeholder bodies and pinned signatures;
their owners replace the bodies.

| Module | Owner | Signatures |
|---|---|---|
| `perception` | WS1 | `card(facts, *, tier, checks=None, motion=None, look=None) -> str`; `diff(prev, cur, *, checks_prev=None, checks_cur=None) -> str`; `stub(k, summary) -> str`; `subjects(facts, roles=None) -> list[str]` |
| `qa` | WS4 | `evaluate(facts, spec=None, motion=None, look=None, previous=None) -> QAReport`; `checklist_text(report, tier, limit=12) -> str`; `report_markdown(report, spec=None, manifest=None) -> str`; `QAReport(score, areas, checks, blocking, resolved, new, regressed, delta)` with `failing`, `passed`, `total`, `to_dict()` |
| `spec` | WS4 | `draft(brief) -> dict`; `validate(spec) -> list[{path, message}]`; `normalize(spec) -> dict` |
| `guard` | WS5 | `prepare(code, tier) -> Prepared(send_code, display_code, lint_notes, line_map)`, raising `GuardRejection`; `interpret(text, prepared) -> (text, is_error)` |
| `actions` | WS5 | `parse(content, allowed=None, max_actions=8) -> list[Action(tool, arguments, done)]`, raising `ActionError` |
| `errors` | shared | `ToolFailure(text)`, `GuardRejection(text)`, `TruncatedReply(chars)`, `ActionError(text)` |

## tools/contract.json

The parallel-development contract: every planned 0.4 tool with its owner,
module, kind, flags and a partial JSON Schema of the arguments that fixes,
recipes and prompts may rely on (names, types, enums, required), plus the
event payloads. It is conservative: a call that passes every pinned
required argument is valid against the real tool.

- `registry.check_args(name, args)` validates a planned call (a WS4 fix, a
  recipe step) against the registered schema, or against the contract while
  the owning workstream has not landed.
- `registry.contract_problems(spec)` lists how a registered spec breaks its
  entry: a pinned argument missing or retyped, a pinned enum value no longer
  accepted, a required argument the contract does not require, or a
  changed `kind`/`readonly`/`destructive`/`hidden` flag.
  `tests/test_registry.py` runs it for every registered tool, so each
  workstream's own test run proves it keeps the contract.

Two notes from the plan: `astra_assemble_parts` keeps its 0.3.2 argument
names (`root_name`, `names`, `anchor`, `max_gap`), and callers keep passing
`ground_name` to `astra_place_on_ground` even if WS3 makes it optional.

## Headless Blender

`astra_blender.headless` runs a persistent
`blender --background --factory-startup --python headless_worker.py` and
speaks JSON lines over stdin and stdout. It opens no socket, so it cannot
touch a live Blender (9876) or an Astra server (8765).

- `HeadlessBlender(exe=None, timeout=170)`: `start()`, `close()`,
  `run(code)`, `reset()` (factory scene), and the upstream-shaped
  `execute_blender_code`, `get_scene_info`, `get_object_info`,
  `get_viewport_screenshot`. A call that passes its timeout kills and
  restarts the worker (the scene is then the factory one again). The
  executable is `ASTRA_BLENDER_EXE`, or the Blender 5.2 default path.
- `headless_connect(config, blender=None, reset=False)`: an engine
  connector. `execute(run, MCPConfig(), provider, connector=headless_connect)`
  runs the whole loop offline. Pass `vision=False`: background Blender has no
  viewport, so `get_viewport_screenshot` fails exactly as upstream does.

Safe mode is enforced on the host before anything is sent; a rejected script
never runs.

## Adding a tool, step by step

1. Write the script in `blender_scripts/` (your workstream's file) with an
   entry function taking the tool's arguments as keywords and returning a
   dict with `"v": 1`. Validate every input before changing the scene.
2. Add a `ToolSpec` to your module's `TOOLS` with schema, stages, tiers,
   flags, `summarize` for mutating tools, and `typical`/`max` examples.
3. Add a post hook if the model should read something shorter than the raw
   JSON, or if files and events should be written.
4. Run `pytest tests/test_registry.py tests/test_tool_safety.py`: they pick
   the tool up automatically (schema rules, examples, contract, safe mode at
   typical, max and hostile arguments).
5. Run the script for real: a test marked `@pytest.mark.blender` using the
   `headless` fixture (`headless.execute_blender_code(scripts.build(spec,
   args))`, then `scripts.parse`).

## Tests and fixtures

`tests/conftest.py` provides, as plain helpers and as fixtures:
`FakeSession(payloads)` (answers a trusted script by the entry function it
calls), `ScriptedProvider(responses)`, `action(name, args, ...)`,
`run_with(tmp_path, provider, session, **config)`, `token_headers(client)`,
`blender_exe` (skips without Blender), `headless_blender` (one per session)
and `headless` (reset to the factory scene per test).

Markers: `blender` (headless Blender 5.2; skipped when absent) and `live`
(a disposable live Blender MCP session). CI runs
`pytest -m "not blender and not live"`; run `pytest -m blender` locally.
