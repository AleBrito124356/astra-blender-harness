---
name: astra-blender
description: Builds, edits, frames, animates and checks Blender scenes through the Astra MCP tools (run_python with Astra Guard and an after-edit audit, inspect_scene, frame_camera, assemble_parts, place_on_ground, keyframe_object, inspect_animation, checkpoint/restore_checkpoint, look, quality_check). Use whenever the user asks for 3D work in Blender - modeling a scene or product, fixing a .blend, lighting, camera framing, turntables or simple animation - or mentions Blender, bpy or a .blend file. Works with a desktop Blender running the Blender MCP add-on or a headless Blender.
license: MIT
compatibility: Needs the astra-blender MCP server (`astra-blender mcp`, Python 3.11+). Blender comes from the Blender MCP add-on in a running Blender, or a headless Blender (the bpy wheel on Python 3.13, or a Blender executable).
metadata:
  version: "0.4.0"
  homepage: "https://github.com/AleBrito124356/astra-blender-harness"
---

# Building in Blender with Astra

Astra sits between you and Blender. It refuses dangerous scripts before they run,
audits the scene after every edit, and gives you reviewed tools for the things
models most often get wrong. Its numbers are evidence; your job is the taste.

## The loop

1. **Orient.** `blender_status` (version, backend, viewer URL), then `inspect_scene`.
   Preserve existing work unless the user asked to replace it.
2. **Plan before building.** Write 3-6 acceptance criteria the user would agree
   with: subject and its real-world size in metres, composition, materials,
   lighting, and the deliverable (still, turntable, animation, .blend only).
3. **Build in small `run_python` steps.** One idea per call (a part, a material,
   a light rig). Put new objects in an `ASTRA` collection with meaningful names,
   share materials, use real-world scale, add bevels where light should catch.
   Print what you need to know - the printout comes back to you.
4. **Read the audit every time.** Each edit returns `New:` and `Resolved:`
   problems (no material, outside or cropped by the frame, below the ground,
   detached parts, nested bounds). Fix what you introduced before moving on. If
   Python raised, earlier lines may already have run: inspect before retrying.
5. **Use the reviewed tools instead of hand-written math.**
   - `frame_camera` with the exact subject names (never floors or backdrops).
   - `assemble_parts` then `place_on_ground` for anything with parts.
   - `keyframe_object` (local space, radians) and `inspect_animation` for motion;
     pass `frames` to `frame_camera` so the whole motion stays in frame.
6. **Checkpoint** before risky edits; `restore_checkpoint` when an edit went wrong.
   Never save or open `.blend` files from `run_python` - Astra does that.
7. **Review.** `quality_check` when available (Blender Quality Lab's gates and a
   "Fix first" list with ready-to-run Python), otherwise `inspect_scene`. Use
   `look` if you can see images; say so plainly if you cannot.
8. **Report honestly.** What exists, the evidence for each criterion, and what
   is still weak. Passing checks is not proof of a beautiful image.

## Rules that save time

- Guard refusals name the line and the reason. Rewrite with `bpy`, `bmesh`,
  `mathutils`, `math`, `random`, `json`, `numpy` and plain Python. No `os`,
  `sys`, `subprocess`, `open`, `eval`/`exec`, dunder attributes, handlers,
  timers, `register_class` or file open/save operators.
- Query the API when unsure (`print(dir(...))`, `bpy.app.version`) instead of
  guessing; Blender 4.x/5.x renamed sockets (Principled "Transmission Weight",
  "Emission Color") and moved animation to layered actions.
- Rendering: EEVEE or Cycles with modest samples, to a path you report. Never
  start an unbounded or full-animation render unless asked.
- A headless Blender's `look` is a quick preview through the scene camera, not
  the user's viewport. The user can watch the live 3D viewer at the URL from
  `blender_status`.
