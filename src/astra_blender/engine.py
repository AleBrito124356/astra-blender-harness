import asyncio
import base64
import json
import re
import time
import uuid
from pathlib import Path

from jsonschema import ValidationError, validate

from . import motion, references, spatial
from .bridge import connect, discover
from .config import MCPConfig, RunConfig
from .history import repair_history
from .prompts import STAGES, SYSTEM
from .provider import LiteLLMProvider, parse_json_action

# A budget field set to zero means "no limit"; this stands in for infinity so
# the comparisons and range() below stay ordinary integer arithmetic.
UNCAPPED = 10**9

# Shorter than this is not a credential worth matching against every string.
MIN_SECRET_LENGTH = 8


def budget(value):
    return UNCAPPED if not value else value


def _build_share(config):
    """Turns the build phase may use when no explicit cap is set."""
    return UNCAPPED


def without_images(messages):
    """Drop image payloads before persisting: they are large, and a resumed run
    captures fresh viewport evidence anyway."""
    lean = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            kept = [block for block in content if block.get("type") != "image_url"]
            message = {**message, "content": kept or "Image evidence omitted from the saved state."}
        lean.append(message)
    return lean


READ_ONLY = {
    "get_scene_info",
    "get_object_info",
    "get_viewport_screenshot",
    "astra_inspect_scene",
    "astra_inspect_animation",
}
TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted"}


class AnimationIncomplete(RuntimeError):
    pass


class BudgetExceeded(Exception):
    pass


class Run:
    def __init__(self, config: RunConfig, root: Path):
        self.id = uuid.uuid4().hex
        self.config = config
        self.directory = (root / self.id).resolve()
        self.directory.mkdir(parents=True)
        self.status = "queued"
        self.events = []
        self.task = None
        self.approvals = {}
        self.steps = 0
        self.total_tokens = 0
        self.created_at = time.time()
        # Set by execute(); lets a human approval pause the run deadline.
        self.deadline = None
        self.waited_for_approval = 0.0
        # The live conversation and phase, so execute() can persist a run
        # that stopped early without reaching back into the loop.
        self.messages = []
        self.current_stage = None

    def clean(self, value):
        if isinstance(value, str):
            key = self.config.api_key.get_secret_value()
            # Only redact something long enough to be a real credential: a one
            # or two character value matches everywhere and would shred the
            # trace, turning "execute_blender_code" into "e[REDACTED]ecute...".
            if len(key) >= MIN_SECRET_LENGTH:
                value = value.replace(key, "[REDACTED]")
            return re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[REDACTED]", value)
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        return value

    def emit(self, kind, **data):
        event = self.clean({"index": len(self.events), "time": time.time(), "type": kind, **data})
        self.events.append(event)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_error(self, error):
        """Keep a redacted traceback beside the run, for diagnosis after the fact."""
        import traceback

        body = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        try:
            (self.directory / "error.log").write_text(self.clean(body), encoding="utf-8")
        except OSError:
            pass

    def save_state(self, messages, stage):
        """Persist enough to continue this run later. The scene itself stays in
        Blender, so a resumed run re-inspects rather than trusting this copy."""
        state = {
            "schema_version": 1,
            "stage": stage,
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "prompt": self.config.prompt,
            "quality": self.config.quality,
            "model": self.config.model,
            "animation": self.config.animation,
            "animation_frames": self.config.animation_frames,
            "animation_fps": self.config.animation_fps,
            "messages": without_images(messages),
        }
        try:
            temporary = self.directory / "state.tmp"
            temporary.write_text(
                json.dumps(self.clean(state), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.directory / "state.json")
        except OSError:
            pass

    def snapshot(self, after=0):
        return {
            "id": self.id,
            "status": self.status,
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "events": self.events[after:],
            "cursor": len(self.events),
        }

    async def approve(self, name, arguments):
        if name in READ_ONLY or self.config.auto_approve:
            return True
        approval_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.approvals[approval_id] = future
        self.emit("approval", approval_id=approval_id, tool=name, arguments=arguments)
        self.status = "awaiting_approval"
        # Human review time is not the model's time. Suspend the deadline for as
        # long as the operation sits unanswered, then restore it shifted by the
        # wait. Compensating afterwards would be too late: the timeout fires
        # while the person is still reading, which is how a healthy run once
        # died 16 minutes into an unread approval.
        resume_at = self.deadline.when() if self.deadline is not None else None
        if resume_at is not None:
            self.deadline.reschedule(None)
        started = time.monotonic()
        try:
            return await future
        finally:
            self.approvals.pop(approval_id, None)
            self.status = "running"
            waited = time.monotonic() - started
            self.waited_for_approval += waited
            if resume_at is not None:
                self.deadline.reschedule(resume_at + waited)
                # Filtered out of the activity feed, kept in events.jsonl.
                self.emit("deadline", waited_seconds=round(waited, 1))


def first_cause(error):
    """Dig the exception Astra raised out of a task group's ExceptionGroup.

    The MCP client runs inside anyio task groups, which repackage anything
    raised through them - twice over, in practice. Without this, a phase that
    merely ran out of turns surfaces as a generic connection failure and sends
    the reader off checking their API key.
    """
    if not isinstance(error, BaseExceptionGroup):
        return error
    leaves = []

    def walk(group):
        for item in group.exceptions:
            walk(item) if isinstance(item, BaseExceptionGroup) else leaves.append(item)

    walk(error)
    # Prefer the reasons Astra raises deliberately over transport noise the
    # group may have collected while unwinding.
    for kind in (BudgetExceeded, asyncio.CancelledError, TimeoutError):
        for leaf in leaves:
            if isinstance(leaf, kind):
                return leaf
    return leaves[0] if leaves else error


def result_parts(result):
    texts, images = [], []
    for block in result.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "image":
            images.append(block)
    if getattr(result, "structuredContent", None):
        texts.append(json.dumps(result.structuredContent))
    failed = result.isError or any(re.match(r"(?i)^\s*(error\b|failed\b)", t) for t in texts)
    prefix = "TOOL ERROR: " if failed else ""
    return prefix + "\n".join(texts)[:20000], images


async def execute(run: Run, mcp_config: MCPConfig, provider=None, connector=connect, state=None):
    provider = provider or LiteLLMProvider(run.config)
    run.status = "running"
    run.emit(
        "started",
        model=run.config.model,
        quality=run.config.quality,
        prompt=run.config.prompt,
        output_dir=str(run.directory),
    )
    try:
        # timeout(None) is asyncio's own way to say "no deadline".
        async with asyncio.timeout(run.config.timeout_seconds or None) as deadline:
            run.deadline = deadline
            async with connector(mcp_config) as session:
                tools = await discover(session, mcp_config.allowed_tools)
                run.emit("connected", tools=list(tools))
                await _loop(run, session, tools, provider, state)
        run.status = "completed"
        run.emit("completed", message="Quality loop finished. Inspect the scene before production use.")
    except (Exception, asyncio.CancelledError, BaseExceptionGroup) as error:
        cause = first_cause(error)
        # The full traceback goes to disk, redacted, rather than to the browser:
        # provider exception bodies can carry credentials and request payloads.
        run.write_error(error)
        if isinstance(cause, asyncio.CancelledError):
            run.status = "cancelled"
            run.emit(
                "cancelled",
                message="Stopped sending commands. An in-flight Blender operation may still finish.",
            )
        elif isinstance(cause, BudgetExceeded):
            run.status = "budget_exhausted"
            run.emit("budget_exhausted", message=str(cause))
        elif isinstance(cause, AnimationIncomplete):
            run.status = "failed"
            run.emit("failed", message=str(cause))
        elif isinstance(cause, TimeoutError):
            run.status = "failed"
            run.emit(
                "failed",
                message="TimeoutError: the run passed its time limit while working. "
                "Waiting for your approval does not count towards it, so raise the run "
                "timeout or simplify the brief. A Blender operation may still be running.",
            )
        else:
            run.status = "failed"
            run.emit(
                "failed",
                message=f"{type(cause).__name__}: connection, provider or tool failed. "
                "Check your model ID, API credentials, MCP configuration and Blender. "
                "See error.log in this run's files for the redacted traceback.",
            )
    finally:
        # Saved on success and failure alike: a run that stopped early is
        # exactly the one worth continuing.
        if run.messages:
            run.save_state(run.messages, run.current_stage or STAGES[0][0])
        manifest = {
            "schema_version": 1,
            "id": run.id,
            "status": run.status,
            "model": run.config.model,
            "quality": run.config.quality,
            "steps": run.steps,
            "total_tokens": run.total_tokens,
            "elapsed_seconds": round(time.time() - run.created_at, 2),
            "awaiting_approval_seconds": round(run.waited_for_approval, 2),
            "files": sorted(p.name for p in run.directory.iterdir() if p.is_file()),
        }
        (run.directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


async def _loop(run, session, tools, provider, state=None):
    tools = {**tools, **{tool.name: tool for tool in spatial.tools()}}
    messages = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": f"BRIEF: {run.config.prompt}\nQUALITY: {run.config.quality}\noutput_dir: {run.directory.as_posix()}",
        },
    ]
    if state:
        # A resumed run inherits the earlier conversation, which carries the
        # plan and what was already built.
        messages = repair_history(state.get("messages") or [], vision=run.config.vision)
        if messages and messages[0].get("role") == "system":
            messages[0] = {"role": "system", "content": SYSTEM}
        messages.append(
            {
                "role": "user",
                "content": "RESUMING this run with a fresh budget. Your earlier work is already "
                "in the Blender scene, so inspect the current state first and continue from "
                "there. Do not rebuild what exists. New output_dir: " + run.directory.as_posix(),
            }
        )
    messages.append(
        {
            "role": "user",
            "content": "Model vision is "
            + (
                "enabled. Request viewport images when useful."
                if run.config.vision
                else "DISABLED. Use astra_inspect_scene for spatial evidence. Never claim to see images. The human has an independent live 3D viewer."
            ),
        }
    )
    reference = references.message(run.directory)
    if reference:
        if not run.config.vision:
            raise ValueError("Reference photos require vision")
        messages.append(reference)
    animated = run.config.wants_animation() or bool(state and state.get("animation") == "on")
    if animated:
        run.config.animation = "on"
        messages.append(
            {
                "role": "user",
                "content": f"ANIMATION DELIVERABLE: {run.config.animation_frames} frames at {run.config.animation_fps} fps. "
                "Plan a parts/parent/pivot table before building. Keep body, glass, trim, wheels and joints distinct. "
                "Assemble and check ground/contact before keyframes. Use a common Empty root for shared motion; "
                "only articulate parts that need relative motion. Inspect start/middle/end and key motion extremes. "
                "For vision, also view screenshots at representative frames using bpy scene.frame_set. "
                "Save editable animation in the .blend; do not start a full animation render unless explicitly requested. "
                "Do not claim smooth or collision-free motion from three numerical samples alone.",
            }
        )
    run.messages = messages
    image_count = 0

    async def call(name, args, *, internal=False, readonly=False):
        nonlocal image_count
        if name not in tools:
            return "TOOL ERROR: tool is unavailable or not allowed", []
        if name == "get_viewport_screenshot" and not run.config.vision:
            return (
                "TOOL ERROR: vision disabled; use astra_inspect_scene. The human live viewer is independent.",
                [],
            )
        if readonly and name not in READ_ONLY:
            return "TOOL ERROR: this phase is read-only", []
        properties = tools[name].inputSchema.get("properties", {})
        if isinstance(args, dict) and "user_prompt" in properties:
            args = {**args, "user_prompt": run.config.prompt}
        # Capture above the server's default so the saved evidence is worth
        # inspecting at full size; an explicit model choice still wins.
        if isinstance(args, dict) and "max_size" in properties and "max_size" not in args:
            args = {**args, "max_size": run.config.screenshot_max_size}
        try:
            validate(args, tools[name].inputSchema)
        except ValidationError as error:
            return "TOOL ERROR: invalid arguments: " + error.message[:500], []
        if not await run.approve(name, args):
            run.emit("denied", tool=name)
            return "TOOL ERROR: user denied this action; do not repeat it", []
        run.emit("tool_call", tool=name, arguments=args, internal=internal)
        # Do not automatically retry a mutation: its result may be uncertain after timeout.
        if name in {"astra_inspect_scene", "astra_frame_camera", *spatial.MOTION_FUNCTIONS}:
            code = spatial.tool_code(name, args)
            backend_args = {"code": code}
            if "user_prompt" in tools["execute_blender_code"].inputSchema.get("properties", {}):
                backend_args["user_prompt"] = run.config.prompt
            result = await session.call_tool("execute_blender_code", backend_args)
            text, images = result_parts(result)
            if name == "astra_inspect_scene" and not text.startswith("TOOL ERROR:"):
                report = spatial.diagnostics(spatial.parse_probe(result))
                text = json.dumps(report, ensure_ascii=False)
                (run.directory / "quality.json").write_text(run.clean(text), encoding="utf-8")
                run.emit(
                    "quality",
                    issues=report["issues"],
                    message=str(len(report["issues"])) + " scene checks need review",
                )
            if name == "astra_inspect_animation" and not text.startswith("TOOL ERROR:"):
                report = motion.report(spatial.parse_probe(result), spatial.diagnostics)
                text = json.dumps(report, ensure_ascii=False)
                (run.directory / "animation.json").write_text(run.clean(text), encoding="utf-8")
                run.emit(
                    "animation", message="Evaluated animation poses inspected", actions=report["actions"]
                )
        else:
            result = await session.call_tool(name, args)
            text, images = result_parts(result)
        failed = text.startswith("TOOL ERROR:")
        run.emit("tool_result", tool=name, text=text, is_error=failed)
        if internal and failed:
            raise RuntimeError(f"Required {name} operation failed")
        image_messages = []
        for block in images[:2]:
            if block.mimeType not in {"image/png", "image/jpeg", "image/webp"}:
                continue
            if len(block.data) > 12_000_000:
                continue
            raw = base64.b64decode(block.data, validate=True)
            image_count += 1
            suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[block.mimeType]
            filename = f"viewport-{image_count:03}.{suffix}"
            (run.directory / filename).write_bytes(raw)
            run.emit("image", file=filename, tool=name)
            if run.config.vision:
                image_messages.append(
                    {"type": "image_url", "image_url": {"url": f"data:{block.mimeType};base64,{block.data}"}}
                )
        return text, image_messages

    async def evidence():
        text, _ = await call("get_scene_info", {}, internal=True)
        messages.append({"role": "user", "content": "Scene evidence:\n" + text})
        text, _ = await call("astra_inspect_scene", {}, internal=True)
        messages.append({"role": "user", "content": "Numerical scene audit:\n" + text})
        if run.config.vision:
            text, images = await call("get_viewport_screenshot", {}, internal=True)
            content = [{"type": "text", "text": "Current Blender viewport. " + text}] + images
            messages.append({"role": "user", "content": content})

    async def save(filename):
        if getattr(session, "simulated", False):
            run.emit("simulation", message=f"DEMO: would save {filename}; no Blender file created")
            return
        path = (run.directory / filename).as_posix()
        code = (
            "import bpy\nbpy.ops.wm.save_as_mainfile(filepath="
            + repr(path)
            + ", copy=True)\nprint('Saved scene copy')"
        )
        text, _ = await call("execute_blender_code", {"code": code}, internal=True)
        if text.startswith("TOOL ERROR"):
            raise RuntimeError("Required scene save was denied or failed")
        if not (run.directory / filename).is_file():
            raise RuntimeError("Reported scene save is missing from the shared output directory")
        run.emit("artifact", file=filename, message="Scene copy verified on disk")

    await evidence()
    await save("checkpoint.blend")
    stages = STAGES if run.config.quality != "draft" else [s for s in STAGES if s[0] != "refine"]
    names = [s[0] for s in stages]
    if state and state.get("stage") in names:
        stages = stages[names.index(state["stage"]) :]
        run.emit("resumed", stage=state["stage"], previous_steps=state.get("steps", 0))
    for stage, instruction in stages:
        run.current_stage = stage
        run.emit("phase", phase=stage)
        messages.append({"role": "user", "content": f"PHASE {stage.upper()}: {instruction}"})
        readonly = stage in {"plan", "review"}
        offered = [
            dict(
                type="function",
                function=dict(name=t.name, description=t.description or t.name, parameters=t.inputSchema),
            )
            for t in tools.values()
            if (not readonly or t.name in READ_ONLY)
            and (run.config.vision or t.name != "get_viewport_screenshot")
        ]
        if run.config.tool_mode == "json":
            messages.append(
                {
                    "role": "user",
                    "content": 'Respond with exactly one JSON object: {"tool":"name","arguments":{...}} '
                    'or {"done":"phase summary"}. Available tools: ' + json.dumps(offered),
                }
            )
        # Mutation phases need an action turn and a turn to assess its result.
        later = stages[[s[0] for s in stages].index(stage) + 1 :]
        remaining = sum(1 if name in {"plan", "review"} else 2 for name, _ in later)
        total = budget(run.config.max_steps)
        phase_limit = max(1, total - run.steps - remaining)
        if stage in {"plan", "review"}:
            phase_limit = min(phase_limit, run.config.inspect_max_steps)
        elif stage == "build":
            phase_limit = min(phase_limit, run.config.build_max_steps or _build_share(run.config))
        for phase_turn in range(phase_limit):
            if run.steps >= total or run.total_tokens >= budget(run.config.max_total_tokens):
                raise BudgetExceeded(
                    "Model-turn or token budget reached; the scene is incomplete. "
                    "Continue this run, or raise the budgets and start again."
                )
            phase_capped = (
                run.config.max_steps or readonly or (stage == "build" and run.config.build_max_steps)
            )
            if phase_capped or run.config.max_total_tokens:
                turns_left = str(phase_limit - phase_turn) if phase_capped else "unlimited"
                tokens_left = (
                    str(max(0, run.config.max_total_tokens - run.total_tokens))
                    if run.config.max_total_tokens
                    else "unlimited"
                )
                messages.append(
                    {
                        "role": "user",
                        "content": f"Budget reminder: {turns_left} model turns remain in this phase, including this response. "
                        f"Reported-token allowance left: {tokens_left}. Reserve a response to assess tool results "
                        "and finish the phase. Prioritize the brief, spatial correctness and camera over extra detail.",
                    }
                )
            run.steps += 1
            message, usage = await provider.complete(
                [{k: v for k, v in m.items() if k != "astra_reference"} for m in messages], offered
            )
            run.total_tokens += usage.get("total_tokens", 0) or 0
            run.emit("usage", steps=run.steps, total_tokens=run.total_tokens)
            calls = []
            if run.config.tool_mode == "json":
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                try:
                    name, arguments = parse_json_action(message.get("content") or "")
                except (ValueError, TypeError) as error:
                    messages.append({"role": "user", "content": f"Invalid action. {error}. Try again."})
                    run.emit("repair", message="Requested valid JSON action")
                    continue
                if name is None:
                    run.emit("assistant", text=arguments)
                    break
                calls = [
                    {"id": uuid.uuid4().hex, "function": {"name": name, "arguments": json.dumps(arguments)}}
                ]
            else:
                messages.append(message)
                calls = message.get("tool_calls") or []
                if message.get("content"):
                    run.emit("assistant", text=message["content"])
                if not calls:
                    if not str(message.get("content") or "").strip():
                        messages.append(
                            {
                                "role": "user",
                                "content": "Empty response. Return a tool action or a useful phase summary.",
                            }
                        )
                        run.emit("repair", message="Requested a non-empty phase result")
                        continue
                    break
            images = []
            mutated = False
            run.save_state(messages, stage)
            for index, tool_call in enumerate(calls):
                function = tool_call["function"]
                try:
                    arguments = json.loads(function["arguments"])
                    if index >= 8:
                        text, pictures = "TOOL ERROR: maximum 8 tool calls per turn", []
                    else:
                        text, pictures = await call(function["name"], arguments, readonly=readonly)
                except (json.JSONDecodeError, TypeError):
                    text, pictures = "TOOL ERROR: arguments must be a JSON object", []
                if run.config.tool_mode == "native":
                    messages.append(
                        {"role": "tool", "tool_call_id": tool_call["id"], "content": text or "Image captured"}
                    )
                else:
                    messages.append(
                        {"role": "user", "content": "Tool result:\n" + (text or "Image captured")}
                    )
                images.extend(pictures)
                # Failed Python may have changed half the scene before raising.
                mutated |= function["name"] in tools and function["name"] not in READ_ONLY and not readonly
                run.save_state(messages, stage)
            if images:
                messages.append(
                    {"role": "user", "content": [{"type": "text", "text": "Tool image evidence"}] + images}
                )
            if mutated:
                audit, _ = await call("astra_inspect_scene", {}, internal=True)
                messages.append({"role": "user", "content": "After-edit spatial checks:\n" + audit})
            run.save_state(messages, stage)
            # Retain only the latest image-bearing message; keep all text/tool pairs intact.
            seen_image = False
            for old in reversed(messages):
                if old.get("astra_reference"):
                    continue
                if isinstance(old.get("content"), list) and any(
                    b.get("type") == "image_url" for b in old["content"]
                ):
                    if seen_image:
                        old["content"] = [b for b in old["content"] if b.get("type") != "image_url"]
                    seen_image = True
        else:
            raise BudgetExceeded(
                f"Phase '{stage}' used all its model turns; the scene is incomplete. "
                "Continue with a larger total or per-phase budget. Automatic build budgeting reserves turns for finishing."
            )
        if stage in {"build", "refine"}:
            if animated:
                text, _ = await call("astra_inspect_animation", {}, internal=True)
                messages.append({"role": "user", "content": "Motion evidence:\n" + text})
            await evidence()
            await save(f"{stage}.blend")
    if animated:
        animation_text, _ = await call("astra_inspect_animation", {}, internal=True)
        if not any(
            action.get("changing_channels") for action in json.loads(animation_text).get("actions", [])
        ):
            raise AnimationIncomplete(
                "Animation was requested but no active action with changing keyframe values was found. "
                "Continue and create the motion; NLA-only or procedural rigs need explicit inspection."
            )
    audit, _ = await call("astra_inspect_scene", {}, internal=True)
    messages.append({"role": "user", "content": "Final scene audit:\n" + audit})
    await save("scene.blend")
