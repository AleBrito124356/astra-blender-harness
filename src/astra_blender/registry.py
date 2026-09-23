"""The trusted-tool registry: one ToolSpec per tool, discovered from tools/.

Each module named in tools.TOOL_MODULES exports three names:

    TOOLS         a list of ToolSpec
    PROMPT_CARDS  {tier: text} - what a model must know to use those tools
    RECIPES       a list of Recipe - ordered calls for common briefs

A workstream adds a tool by filling its own module. The registration line
is already in tools/__init__.py, so no shared file changes. The engine
derives its tool pool, READ_ONLY set, dispatch and evidence from here;
docs/tool-contract.md explains every field.
"""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from . import scripts

STAGES = ("plan", "layout", "look", "motion", "review", "refine", "finalize")
TIERS = ("small", "medium", "large")
KINDS = ("blender", "harness")
# Upstream blender-mcp tools that never change the scene.
UPSTREAM_READ_ONLY = frozenset({"get_scene_info", "get_object_info", "get_viewport_screenshot"})
# Injected names the engine can fill without a spec-specific hook.
STANDARD_INJECTED = ("output_dir", "save_to", "start_from", "budget_s", "known")
# Longest description a model is shown, in words (units included).
DESCRIPTION_WORDS = 60
CONTRACT = Path(__file__).parent / "tools" / "contract.json"


class RegistryError(Exception):
    """A tools module declared something the registry cannot accept."""


@dataclass(frozen=True)
class PostResult:
    """What a post hook or a harness handler hands back to the engine.

    text      what the model reads as the tool result
    files     {plain file name: str | bytes} written into the run directory;
              text is redacted through Run.clean first
    events    ((kind, {payload}), ...) emitted in order, before tool_result
    snapshot  any value the engine keeps as this tool's latest snapshot
              (Run.tool_state["snapshots"][tool]) - WS6 diffs against it
    """

    text: str
    files: Mapping[str, str | bytes] = field(default_factory=dict)
    events: tuple = ()
    snapshot: Any = None


@dataclass
class PostContext:
    """What a post hook, inject hook or harness handler may use.

    tier     'small' | 'medium' | 'large' - the model's profile tier
    run_dir  the run directory (Path)
    state    a dict that lives for the whole run, shared by every tool
    clean    Run.clean: redacts credentials from str/dict/list values
    emit     Run.emit(kind, **payload)
    call     async call(name, args) -> ToolOutcome; read-only registry
             tools only (it raises ToolFailure otherwise). The nested call
             runs internally: no approval, no model turn.
    config   the RunConfig
    tool     the name of the tool being run
    raw      the raw result text of a blender tool, once it exists
    """

    tier: str
    run_dir: Path
    state: dict
    clean: Callable
    emit: Callable
    call: Callable
    config: Any
    tool: str = ""
    raw: str = ""


@dataclass
class ToolOutcome:
    """One finished tool call, as the engine and ctx.call see it.

    executed is False when nothing reached Blender: unknown tool, read-only
    violation, invalid or non-finite arguments, denial, safe-mode rejection.
    data holds the parsed marker JSON of a blender tool.
    """

    name: str
    text: str
    is_error: bool
    executed: bool
    data: Any = None
    snapshot: Any = None
    images: list = field(default_factory=list)


@dataclass(frozen=True)
class Recipe:
    """An ordered list of tool calls for a common brief.

    keywords  lower-case words in English and Spanish that select it
    steps     ((tool, args_template), ...). A string value that is exactly
              "{name}" is replaced by bindings[name] (any JSON type); other
              strings are str.format()-ed with the bindings.
    defaults  example bindings; the registry test renders every recipe with
              them and validates each step against the tool's schema, or
              against tools/contract.json while the tool's owner has not
              landed yet.
    min_tier  the smallest tier the recipe is offered to
    """

    id: str
    keywords: tuple = ()
    steps: tuple = ()
    defaults: Mapping = field(default_factory=dict)
    min_tier: str = "small"
    title: str = ""


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
    """One trusted tool. docs/tool-contract.md is the long form of this."""

    name: str
    # What the model reads. At most 60 words, with units (m, degrees, frames).
    description: str
    # JSON Schema of the model's arguments: an object, additionalProperties
    # false, never user_prompt, never an injected name.
    schema: Mapping
    # Optional reduced schema offered to the small tier instead.
    schema_small: Mapping | None = None
    kind: str = "blender"
    # blender: files in blender_scripts/, concatenated after common.py (and
    # measure.py first when listed); `entry` is the function the trailer
    # calls with the arguments as keywords.
    scripts: tuple = ()
    entry: str | None = None
    # common=False keeps a 0.3.2 script byte-identical (no common.py).
    common: bool = True
    # harness: async handler(args, ctx) -> PostResult; never touches Blender
    # except through ctx.call.
    handler: Callable | None = None
    readonly: bool = False
    # Always asks for approval (enforced by WS6's approval policy); autofix
    # never applies it.
    destructive: bool = False
    # Counted against a per-run budget (WS6).
    expensive: bool = False
    stages: tuple = STAGES
    tiers: tuple = TIERS
    # None: offered with or without vision. True: vision models only.
    # False: text-only models only.
    vision: bool | None = None
    # Lower sorts first when a menu is cut by `limit`; ties keep module order.
    priority: int = 100
    # Harness-only argument names added after validation (never in schema).
    injected: tuple = ()
    # inject(args, ctx) -> {name: value} for injected names beyond the
    # standard ones, or to override them (e.g. a look-NNN.png save_to).
    inject: Callable | None = None
    # A chunked tool returns data[chunk_key] until it is done; the engine
    # calls it again with start_from=<that value> inside one model call.
    chunk_key: str | None = None
    # Seconds one Blender call of this tool may take (injected as budget_s).
    max_seconds: float = 60
    # post(data, ctx) -> PostResult; without it the model reads the raw
    # "Code executed successfully: ASTRA_SCENE_JSON:{...}" text.
    post: Callable | None = None
    # summarize(args) -> one plain sentence for the approval card.
    summarize: Callable | None = None
    # {'typical': {...}, 'max': {...}, optional 'hostile': {...}}; all must
    # validate against schema. test_tool_safety builds each one.
    examples: Mapping = field(default_factory=dict)
    # Accepted when called (resumed runs, old transcripts), never offered.
    hidden: bool = False
    # Filled by load(): the tools module that registered this spec.
    module: str = ""

    def schema_for(self, tier=None):
        if tier == "small" and self.schema_small:
            return copy.deepcopy(dict(self.schema_small))
        return copy.deepcopy(dict(self.schema))

    def as_tool(self, tier=None):
        """The MCP Tool the engine offers and validates against."""
        from mcp.types import Tool

        return Tool(name=self.name, description=self.description, inputSchema=self.schema_for(tier))

    def approval_summary(self, args):
        if self.summarize is None:
            return None
        try:
            return str(self.summarize(args))
        except Exception:  # noqa: BLE001 - a summary must never break an approval
            return None


_state = {"specs": None, "modules": ()}


def structural_problems(spec):
    """Problems that make a spec unusable; load() refuses any of them."""
    problems = []
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", spec.name or ""):
        problems.append("name must be lower_snake_case, 3-64 characters")
    if spec.kind not in KINDS:
        problems.append(f"kind must be one of {KINDS}")
    schema = spec.schema if isinstance(spec.schema, Mapping) else None
    if schema is None or schema.get("type") != "object":
        problems.append("schema must be a JSON Schema object")
    else:
        properties = schema.get("properties", {})
        clash = sorted(set(spec.injected) & set(properties))
        if clash:
            problems.append(f"injected names must not be model arguments: {clash}")
    if not set(spec.stages) <= set(STAGES):
        problems.append(f"stages must be a subset of {STAGES}")
    if not set(spec.tiers) <= set(TIERS):
        problems.append(f"tiers must be a subset of {TIERS}")
    if spec.kind == "blender":
        if not spec.entry or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", spec.entry):
            problems.append("a blender tool needs an entry function name")
        if not spec.scripts:
            problems.append("a blender tool needs its script files")
        for name in spec.scripts:
            if not (scripts.SCRIPTS / name).is_file():
                problems.append(f"missing script blender_scripts/{name}")
        if scripts.MEASURE in spec.scripts and not spec.common:
            problems.append("measure.py relies on common.py; keep common=True")
        if spec.handler is not None:
            problems.append("a blender tool has no handler")
    if spec.kind == "harness":
        if spec.handler is None:
            problems.append("a harness tool needs handler(args, ctx)")
        if spec.scripts or spec.entry:
            problems.append("a harness tool has no Blender scripts")
    unknown = [n for n in spec.injected if n not in STANDARD_INJECTED]
    if unknown and spec.inject is None:
        problems.append(f"injected names {unknown} need an inject(args, ctx) hook")
    if spec.chunk_key and "start_from" not in spec.injected:
        problems.append("a chunked tool must list start_from in injected")
    if spec.readonly and spec.destructive:
        problems.append("a tool cannot be both readonly and destructive")
    return problems


def load(modules=None, *, reload=False):
    """Import every tools module and index its specs. Idempotent.

    Duplicate names across modules raise RegistryError, as does any spec
    with structural problems, so a mistake fails at import, not mid-run.
    An explicit `modules` list replaces the active registry (tests use it
    with throwaway modules and restore registry._state afterwards).
    """
    if _state["specs"] is not None and not reload and modules is None:
        return _state["specs"]
    if modules is None:
        from .tools import TOOL_MODULES

        modules = TOOL_MODULES
    specs, loaded = {}, []
    for module_name in modules:
        module = importlib.import_module(module_name)
        loaded.append(module)
        for spec in getattr(module, "TOOLS", ()):
            if not isinstance(spec, ToolSpec):
                raise RegistryError(f"{module_name}.TOOLS holds a {type(spec).__name__}, not a ToolSpec")
            if spec.name in specs:
                raise RegistryError(
                    f"Duplicate tool name {spec.name!r}: {specs[spec.name].module} and {module_name}"
                )
            problems = structural_problems(spec)
            if problems:
                raise RegistryError(f"{module_name}: {spec.name}: " + "; ".join(problems))
            specs[spec.name] = replace(spec, module=module_name)
    _state["specs"], _state["modules"] = specs, tuple(loaded)
    return specs


def modules():
    load()
    return _state["modules"]


def all_specs():
    return list(load().values())


def get(name):
    try:
        return load()[name]
    except KeyError:
        raise KeyError(f"No registered tool named {name!r}") from None


def find(name):
    return load().get(name)


def names(readonly=None):
    """Every registered name, hidden ones included, in registration order."""
    return [s.name for s in load().values() if readonly is None or s.readonly == readonly]


def hidden_names():
    return {s.name for s in load().values() if s.hidden}


def read_only_names():
    """What never needs approval and may run in read-only phases."""
    return set(UPSTREAM_READ_ONLY) | set(names(readonly=True))


def offered(stage, tier="large", vision=True, limit=None):
    """The specs a model may be offered in one stage, for one tier.

    Hidden specs are never offered. Sorted by priority, then registration
    order; `limit` cuts the menu (WS6 keeps menus at 8 or fewer, 6 small).
    """
    if stage not in STAGES:
        raise ValueError(f"Unknown stage {stage!r}; use one of {STAGES}")
    if tier not in TIERS:
        raise ValueError(f"Unknown tier {tier!r}; use one of {TIERS}")
    chosen = [
        (spec.priority, index, spec)
        for index, spec in enumerate(load().values())
        if not spec.hidden
        and stage in spec.stages
        and tier in spec.tiers
        and (spec.vision is None or spec.vision == bool(vision))
    ]
    chosen.sort(key=_sort_key)
    result = [spec for _, _, spec in chosen]
    return result if limit is None else result[: max(0, int(limit))]


def _sort_key(item):
    return item[0], item[1]


def schema_for(name, tier=None):
    return get(name).schema_for(tier)


def _card_for(cards, tier):
    order = {"large": ("large", "medium", "small"), "medium": ("medium", "small"), "small": ("small",)}
    for candidate in order.get(tier, (tier,)):
        text = cards.get(candidate)
        if text:
            return text.strip()
    return ""


def cards(tier, names=None):
    """PROMPT_CARDS of the modules whose tools are named, in module order.

    A module without a card for this tier falls back to a smaller tier's
    card, never to a bigger one.
    """
    wanted = None if names is None else set(names)
    texts = []
    for module in modules():
        own = {spec.name for spec in getattr(module, "TOOLS", ())}
        if wanted is not None and not own & wanted:
            continue
        text = _card_for(getattr(module, "PROMPT_CARDS", {}) or {}, tier)
        if text:
            texts.append(text)
    return "\n\n".join(texts)


def recipes(tier=None):
    found = []
    for module in modules():
        for recipe in getattr(module, "RECIPES", ()):
            if not isinstance(recipe, Recipe):
                raise RegistryError(f"{module.__name__}.RECIPES holds a {type(recipe).__name__}, not a Recipe")
            if tier is None or TIERS.index(recipe.min_tier) <= TIERS.index(tier):
                found.append(recipe)
    return found


def _bind(value, bindings):
    if isinstance(value, str):
        whole = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        if whole:
            return copy.deepcopy(bindings[whole.group(1)])
        return value.format(**bindings) if "{" in value else value
    if isinstance(value, dict):
        return {key: _bind(item, bindings) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bind(item, bindings) for item in value]
    return value


def render_recipe(recipe, bindings=None):
    """[(tool, arguments)] with the placeholders filled in."""
    values = {**dict(recipe.defaults), **dict(bindings or {})}
    return [(tool, _bind(template, values)) for tool, template in recipe.steps]


def contract():
    """tools/contract.json: the planned 0.4 tools, their pinned arguments, and the events."""
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def check_args(name, args, tier=None):
    """Validation errors for a planned call (a fix, a recipe step), [] when valid.

    Uses the registered schema when the tool exists, else its entry in
    tools/contract.json, so a workstream can validate calls to tools another
    workstream has not landed yet.
    """
    from jsonschema import Draft202012Validator

    spec = find(name)
    if spec is not None:
        schema = spec.schema_for(tier)
    else:
        entry = contract()["tools"].get(name)
        if entry is None:
            return [f"{name} is neither registered nor in tools/contract.json"]
        schema = entry["args"]
    validator = Draft202012Validator(schema)
    return [
        ("/".join(str(p) for p in error.absolute_path) or "(arguments)") + ": " + error.message
        for error in sorted(validator.iter_errors(args), key=str)
    ]


def _types(schema):
    kind = schema.get("type")
    if kind is None:
        return None
    return {kind} if isinstance(kind, str) else set(kind)


def _schema_problems(pinned, real, where):
    problems = []
    pinned_types, real_types = _types(pinned), _types(real)
    if pinned_types and real_types is not None:
        widened = set(real_types)
        if "number" in widened:
            widened.add("integer")
        if not pinned_types <= widened:
            problems.append(f"{where}: type {sorted(pinned_types)} but the tool takes {sorted(real_types)}")
    if "enum" in pinned and "enum" in real and not set(map(json.dumps, pinned["enum"])) <= set(
        map(json.dumps, real["enum"])
    ):
        missing = [v for v in pinned["enum"] if v not in real["enum"]]
        problems.append(f"{where}: enum values {missing} are pinned but not accepted")
    if "properties" in pinned:
        real_properties = real.get("properties", {})
        for key, sub in pinned["properties"].items():
            if key not in real_properties:
                problems.append(f"{where}.{key}: pinned argument is missing")
                continue
            problems += _schema_problems(sub, real_properties[key], f"{where}.{key}")
        extra_required = set(real.get("required", ())) - set(pinned.get("required", ()))
        if extra_required:
            problems.append(f"{where}: requires {sorted(extra_required)}, which the contract does not")
    if isinstance(pinned.get("items"), dict) and isinstance(real.get("items"), dict):
        problems += _schema_problems(pinned["items"], real["items"], f"{where}[]")
    return problems


def contract_problems(spec, entry=None):
    """How a registered spec breaks its tools/contract.json entry ([] if it keeps it).

    A call valid against the contract must stay valid against the tool:
    every pinned argument exists with a compatible type, pinned enum values
    are accepted, and the tool requires nothing the contract does not.
    """
    if entry is None:
        entry = contract()["tools"].get(spec.name)
        if entry is None:
            return []
    problems = []
    for flag in ("kind", "readonly", "destructive", "hidden"):
        if flag in entry and entry[flag] != getattr(spec, flag):
            problems.append(f"{spec.name}: {flag} is {getattr(spec, flag)!r}, contract says {entry[flag]!r}")
    return problems + _schema_problems(entry["args"], dict(spec.schema), spec.name)


def markdown():
    """A generated table of every registered tool, for docs and reviews."""
    lines = [
        "| Tool | Kind | Read-only | Stages | Tiers | Description |",
        "|---|---|---|---|---|---|",
    ]
    for spec in load().values():
        stages = "all" if tuple(spec.stages) == STAGES else ", ".join(spec.stages)
        tiers = "all" if tuple(spec.tiers) == TIERS else ", ".join(spec.tiers)
        flags = "yes" if spec.readonly else ("destructive" if spec.destructive else "no")
        name = spec.name + (" (hidden)" if spec.hidden else "")
        lines.append(
            f"| {name} | {spec.kind} | {flags} | {stages} | {tiers} | {spec.description.replace('|', '/')} |"
        )
    return "\n".join(lines)


async def maybe_await(value):
    """Let post, inject and handler hooks be plain or async functions."""
    if inspect.isawaitable(value):
        return await value
    return value
