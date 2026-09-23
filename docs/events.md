# Run events (Astra 0.4)

Every run writes its events, one JSON object per line, to
`<run>/events.jsonl`, and the server streams the same objects to the browser
(`/api/runs/{id}` with a cursor). Each event is:

```json
{"index": 12, "time": 1790000000.0, "type": "tool_result", "...": "payload"}
```

`index` counts from 0 in emission order; `time` is Unix seconds. Every
payload passes through `Run.clean` before it is stored, so an API key or a
bearer token never reaches the file or the browser.

The payloads below are pinned: the UI, the CLI and the benchmark read them,
and `tools/contract.json` ("events") holds the same list for tests. A field
marked `?` may be absent. "Owner" is who emits it; "since 0.3" events exist
today, "0.4" events are added by the named workstream.

## Run lifecycle

| Event | Payload | Owner | Since |
|---|---|---|---|
| `started` | `model`, `quality`, `prompt`, `output_dir` | engine | 0.3 |
| `connected` | `tools` (discovered upstream tool names) | engine | 0.3 |
| `resumed` | `stage`, `previous_steps` | engine | 0.3 |
| `phase` | `phase` | engine / WS6 | 0.3; 0.4 phases are `plan, layout, look, motion, review, refine, finalize` |
| `usage` | `steps`, `total_tokens` | engine | 0.3 |
| `assistant` | `text` | engine | 0.3 |
| `repair` | `message` | engine | 0.3 |
| `deadline` | `waited_seconds` (a human approval paused the run deadline) | engine | 0.3 |
| `simulation` | `message` (demo session: nothing saved) | engine | 0.3 |
| `demo` | `message` | server | 0.3 |
| `completed`, `failed`, `cancelled`, `budget_exhausted`, `incomplete` | `message` | engine | 0.3 |

## Tool calls

| Event | Payload | Owner | Since |
|---|---|---|---|
| `approval` | `approval_id`, `tool`, `arguments`, `summary?` (ToolSpec.summarize), `code?` (guarded raw Python, WS6) | engine | 0.3; `summary` 0.4 |
| `decision` | `approval_id`, `approved` | server | 0.3 |
| `denied` | `tool` | engine | 0.3 |
| `tool_call` | `tool`, `arguments`, `internal` (true for harness evidence, saves and `ctx.call`) | engine | 0.3 |
| `tool_result` | `tool`, `text`, `is_error` | engine | 0.3 |
| `image` | `file`, `tool` | engine | 0.3 |

Order inside one call: `approval` (unless read-only or auto-approved),
`tool_call`, then the post hook's own events in the order it returns them,
then `tool_result`. A refused call (unknown tool, invalid or non-finite
arguments, read-only phase, denial) emits no `tool_call` and no
`tool_result`; the model still gets a `TOOL ERROR:` message.

## Evidence and files

| Event | Payload | Owner | Since |
|---|---|---|---|
| `quality` | `issues`, `message` (the 0.3.2 audit, quality.json) | tools.sight | 0.3 |
| `animation` | `message`, `actions` (animation.json) | tools.motion | 0.3 |
| `artifact` | `file`, `message?`, `role?`: `primary` \| `scene` \| `report` \| `evidence` \| `trace` | engine / WS2 | 0.3; `role` 0.4 |
| `evidence` | `kind`: `card` \| `changes` \| `stub`, `tier`: `small` \| `medium` \| `large`, `tokens`, `text` | WS1 / WS6 | 0.4 |
| `look` | `file` (look-NNN.png), `engine`, `luma` {p05, p50, p95, mean, rms_contrast}, `clipped_pct`, `crushed_pct`, `exposure_hint_stops` | WS1 | 0.4 |
| `motion` | `range` [start, end], `movers` (names), `issues` (count), `file` (motion.json) | WS1 | 0.4 |
| `spec` | `source`: `draft` \| `model` \| `amended`, `table` (8-20 lines), `file` (spec.json) | WS4 | 0.4 |
| `qa` | `score` (0-100 or null), `areas` {area: 0-100}, `blocking` [ids], `failing` [12 checks at most], `delta` (null on the first check), `file` (qa.json) | WS4 | 0.4 |
| `render` | `kind`: `still` \| `frames` \| `video`, `files`, `progress` [done, total] | WS2 | 0.4 |

A check inside `qa.failing` is `{id, area, level, object, measured,
expected, fix: {tool, arguments} | null, hint}` (see `qa.py`).

## Guard and loop

| Event | Payload | Owner | Since |
|---|---|---|---|
| `guard` | `rule`, `line` (int or null), `hint` | WS5 | 0.4 |
| `autofix` | `check`, `tool`, `arguments` (the harness applied a check's fix itself) | WS6 | 0.4 |
| `regression` | `check`, `call` (a call made a passing check fail) | WS6 | 0.4 |

## Rules for new events

- Emit through `ctx.emit(kind, **payload)` or a `PostResult.events` entry,
  never by writing `events.jsonl` directly.
- Keep payloads small: a file name, counts and at most a dozen items. The
  full data belongs in a file in the run directory (`PostResult.files`).
- Add the event to this page and to `tools/contract.json` ("events") before
  anything reads it.
