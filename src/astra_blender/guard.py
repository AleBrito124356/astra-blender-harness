"""Astra Guard: static checks on model-written Blender Python.

Model-written code reaches Blender through ``execute_blender_code``. Tool
output, asset names and reference images flow into the model's context, and a
prompt-injected model can be talked into writing hostile code that a person
approves without reading every line. The guard rejects the constructs such
code needs before a byte reaches Blender:

* imports outside a short allowlist (no ``os``, ``sys``, ``subprocess``,
  ``socket``, ``importlib``, ``pickle``, ``ctypes``, ``numpy`` ...);
* interpreter escapes: ``eval``/``exec``/``compile``/``__import__``/``open``,
  dunder attributes and names, frame and traceback internals, computed
  ``getattr``/``setattr`` names, ``str.format`` fields that reach into
  attributes, helpers that evaluate strings as code;
* code that outlives the call or loads code: ``bpy.app.handlers``/``timers``,
  ``register_class``, text datablocks, linked or appended .blend libraries,
  script/text/preferences/extensions operators, auto-run of embedded scripts;
* scene-file operators. Astra saves verified copies itself; the model opening,
  reverting or overwriting files would break the checkpoints and could
  overwrite your original ``.blend``.

Three mechanisms keep those rules from being dodged by rewriting a call:

1. Distinctive names (``handlers``, ``save_mainfile``, ``python_file_run`` ...)
   are refused on any receiver, resolvable or not.
2. The namespaces that hold dangerous operators - ``bpy``, ``bpy.ops``,
   ``bpy.ops.<group>``, ``bpy.app``, ``bpy.utils`` - may only be used directly
   (``bpy.ops.mesh.primitive_cube_add()``) or given a plain alias
   (``ops = bpy.ops``). Put in a list, returned from a lambda, passed to a
   function or read with a computed ``getattr`` name, they would hide which
   operator runs, so those forms are refused.
3. Aliases are resolved flow-insensitively: a name stands for every path it is
   ever assigned, so branches and reassignment cannot launder a path.

It is a guard, not a sandbox: Blender's Python is not confined, and code the
guard accepts runs with your user's permissions. Approvals and a disposable
Blender session remain the real boundary. Astra's own reviewed scripts do not
pass through it; they never contain model text.
"""

import ast
import re
import string
from dataclasses import dataclass

ALLOWED_MODULES = frozenset(
    {
        "bpy",
        "bpy_extras",
        "bmesh",
        "mathutils",
        "math",
        "cmath",
        "random",
        "colorsys",
        "json",
        "itertools",
        "functools",
        "collections",
        "statistics",
        "re",
        "enum",
        "dataclasses",
        "decimal",
        "fractions",
        "textwrap",
        "copy",
        "heapq",
        "bisect",
        "array",
        "time",
    }
)
# Deliberately absent: numpy (file and pickle I/O, native library loading -
# too large a surface to fence statically), typing (get_type_hints evaluates
# string annotations), string (Formatter.vformat reaches attributes by name).
# Blender's foreach_get/foreach_set work with array.array or plain lists.

FORBIDDEN_CALLS = {
    "eval": "eval() runs arbitrary expressions",
    "exec": "exec() runs arbitrary code",
    "compile": "compile() builds code objects",
    "__import__": "__import__() bypasses the import allowlist",
    "open": "open() reads and writes any file; use bpy operators for files",
    "input": "input() would block Blender waiting for a terminal",
    "breakpoint": "breakpoint() opens a debugger",
    "exit": "exit() would close Blender",
    "quit": "quit() would close Blender",
    "globals": "globals() exposes the interpreter namespace",
    "locals": "locals() exposes the interpreter namespace",
    "vars": "vars() exposes object internals",
    "help": "help() imports modules through pydoc",
    "memoryview": "memoryview() exposes raw memory",
}
ATTRIBUTE_CALLS = {"getattr", "setattr", "delattr", "hasattr"}
LITERAL_LOOKUPS = {"getattr", "hasattr"}

ALLOWED_DUNDERS = frozenset({"__name__", "__doc__"})

SCENE_FILES = "Astra saves verified scene copies itself; scripts must not open, save or replace .blend files"

# Refused wherever they appear, because the receiver cannot always be resolved
# statically: `x = some_call(); x.handlers` must not slip through.
FORBIDDEN_ATTRIBUTES = {
    "handlers": "bpy.app.handlers persist code beyond this call",
    "timers": "bpy.app.timers persist code beyond this call",
    "driver_namespace": "the driver namespace injects code into every driver",
    "texts": "text datablocks can be run as scripts",
    "libraries": "linked .blend libraries can carry code",
    "register_class": "registered classes persist code beyond this call",
    "unregister_class": "class registration is not allowed",
    "register_classes_factory": "class registration is not allowed",
    "register_submodule_factory": "module registration is not allowed",
    "execfile": "runs a file from disk",
    "load_scripts": "runs scripts from disk",
    "modules_from_path": "imports modules from disk",
    "refresh_script_paths": "reloads scripts from disk",
    "script_directories": "adds a script folder Blender loads code from",
    "script_directory": "adds a script folder Blender loads code from",
    "use_scripts_auto_execute": "enables auto-run of scripts embedded in .blend files",
    "python_file_run": "runs a Python file",
    "run_script": "runs a text datablock as a script",
    "addon_enable": "enables add-ons, which run code",
    "addon_install": "installs add-ons",
    "addon_refresh": "rescans add-on code",
    "package_install": "installs extensions",
    "package_install_files": "installs extensions",
    "save_mainfile": SCENE_FILES,
    "save_as_mainfile": SCENE_FILES,
    "open_mainfile": SCENE_FILES,
    "revert_mainfile": SCENE_FILES,
    "recover_last_session": SCENE_FILES,
    "recover_auto_save": SCENE_FILES,
    "read_homefile": SCENE_FILES,
    "read_factory_settings": SCENE_FILES,
    "read_factory_userpref": "would reset your preferences",
    "save_homefile": "overwrites your startup file",
    "save_userpref": "overwrites your preferences",
    "lib_relocate": "relocating libraries loads other .blend files",
    "lib_reload": "reloading libraries loads other .blend files",
    "url_open": "opens a URL",
    "path_open": "opens a path with the system handler",
    "quit_blender": "would close Blender",
    "play_rendered_anim": "launches an external player",
    "get_type_hints": "evaluates string annotations as code",
    "singledispatch": "evaluates string annotations as code",
    "singledispatchmethod": "evaluates string annotations as code",
    "ForwardRef": "evaluates strings as code",
    "vformat": "format fields can reach interpreter internals",
    "tofile": "writes raw arrays to any path",
    "dump": "pickles to a file",
    "tb_frame": "frames expose interpreter internals",
    "tb_next": "tracebacks expose interpreter internals",
    "f_globals": "frames expose interpreter internals",
    "f_locals": "frames expose interpreter internals",
    "f_builtins": "frames expose interpreter internals",
    "f_back": "frames expose interpreter internals",
    "f_code": "frames expose interpreter internals",
    "gi_frame": "generator frames expose interpreter internals",
    "gi_code": "generator internals are not allowed",
    "cr_frame": "coroutine frames expose interpreter internals",
    "ag_frame": "generator frames expose interpreter internals",
    "co_code": "code objects are not allowed",
}

# Paths whose last name is too common to refuse everywhere (`list.append`).
FORBIDDEN_PATHS = (
    ("bpy.ops.script", "script operators run Python"),
    ("bpy.ops.text", "text operators run text datablocks"),
    ("bpy.ops.preferences", "preferences operators install and enable add-ons"),
    ("bpy.ops.extensions", "extension operators install packages"),
    ("bpy.ops.console", "the console runs arbitrary Python"),
    ("bpy.ops.wm.append", "appending from another .blend can bring its code"),
    ("bpy.ops.wm.link", "linking another .blend can bring its code"),
    ("time.sleep", "sleeping blocks Blender; there is nothing to wait for"),
)

NAMESPACE_USE = (
    "may only be used directly, as in bpy.ops.mesh.primitive_cube_add(), or given a plain alias "
    "(ops = bpy.ops). Putting it in a container, returning it, passing it to a function or reading it "
    "with a computed name hides which operator runs"
)


def sensitive(path):
    """Namespaces whose members include operators the guard refuses."""
    if path in {"bpy", "bpy.ops", "bpy.app", "bpy.utils"}:
        return True
    return path.startswith("bpy.ops.") and path.count(".") == 2


@dataclass(frozen=True)
class Violation:
    line: int
    message: str

    def __str__(self):
        return f"line {self.line}: {self.message}"


class GuardRejected(ValueError):
    def __init__(self, violations):
        self.violations = list(violations)
        shown = "; ".join(str(v) for v in self.violations[:5])
        more = f" (+{len(self.violations) - 5} more)" if len(self.violations) > 5 else ""
        super().__init__(shown + more)


def _matches(path, prefix):
    return path == prefix or path.startswith(prefix + ".")


def _literal_lookup(node, names=LITERAL_LOOKUPS):
    """getattr/hasattr(x, "literal") - read like an attribute access."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in names
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    )


def _format_fields(text, depth=0):
    """Field names in a format string, nested specs included: '{0.x:{1[2]}}' -> ['0.x', '1[2]']."""
    names = []
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        return ["<malformed>"]
    for _, field, spec, _ in parsed:
        if field is not None:
            names.append(field)
        if spec and depth < 3:
            names.extend(_format_fields(spec, depth + 1))
    return names


class _Checker(ast.NodeVisitor):
    def __init__(self, aliases):
        self.violations = []
        # Local name -> every dotted path it is ever bound to.
        self.aliases = aliases
        # Names that only ever hold string constants, from `for name in ("a", "b")`.
        self.literal_names = {}

    def fail(self, node, message):
        self.violations.append(Violation(getattr(node, "lineno", 0), message))

    # -- resolution ---------------------------------------------------------

    def resolve(self, node):
        return resolve(node, self.aliases)

    def check_paths(self, node, paths):
        for path in sorted(paths):
            for prefix, reason in FORBIDDEN_PATHS:
                if _matches(path, prefix):
                    self.fail(node, f"{path} is not allowed: {reason}")
                    return True
        return False

    def check_namespace_use(self, node, paths):
        """A sensitive namespace used as a value instead of as a receiver."""
        hit = sorted(path for path in paths if sensitive(path))
        if not hit or isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
            return
        parent = getattr(node, "_astra_parent", None)
        if isinstance(parent, ast.Attribute) and parent.value is node:
            return
        if _literal_lookup(parent) and parent.args[0] is node:
            return
        if (
            isinstance(parent, ast.Assign)
            and parent.value is node
            and all(isinstance(t, ast.Name) for t in parent.targets)
        ):
            return
        self.fail(node, f"{hit[0]} {NAMESPACE_USE}")

    # -- imports --------------------------------------------------------------

    def check_module(self, node, module):
        root = module.split(".")[0]
        if root not in ALLOWED_MODULES:
            allowed = ", ".join(sorted(ALLOWED_MODULES))
            self.fail(node, f"import of '{module}' is not allowed. Allowed modules: {allowed}")
            return False
        for part in module.split(".")[1:]:
            if part in FORBIDDEN_ATTRIBUTES or part.startswith("__"):
                self.fail(node, f"import of '{module}' is not allowed")
                return False
        return not self.check_paths(node, {module})

    def visit_Import(self, node):
        for alias in node.names:
            self.check_module(node, alias.name)

    def visit_ImportFrom(self, node):
        if node.level:
            self.fail(node, "relative imports are not allowed")
            return
        module = node.module or ""
        if not self.check_module(node, module):
            return
        for alias in node.names:
            path = f"{module}.{alias.name}"
            if alias.name == "*":
                self.fail(node, f"'from {module} import *' hides what is imported; name it")
            elif alias.name.startswith("__"):
                self.fail(node, f"importing {path} is not allowed")
            elif alias.name in FORBIDDEN_ATTRIBUTES:
                self.fail(node, f"importing {path} is not allowed: {FORBIDDEN_ATTRIBUTES[alias.name]}")
            else:
                self.check_paths(node, {path})

    # -- names and attributes --------------------------------------------------

    def visit_Name(self, node):
        if node.id.startswith("__") and node.id not in ALLOWED_DUNDERS:
            self.fail(node, f"the name {node.id} is not allowed")
        elif node.id in FORBIDDEN_CALLS:
            self.fail(node, f"{node.id} is not allowed: {FORBIDDEN_CALLS[node.id]}")
        elif node.id in ATTRIBUTE_CALLS:
            # Passed around as a value (`reduce(getattr, names, obj)`) its
            # attribute name can no longer be checked.
            self.fail(node, f"{node.id} may only be called directly with a literal attribute name")
        else:
            self.check_namespace_use(node, self.resolve(node))

    def visit_Attribute(self, node):
        attr = node.attr
        if attr.startswith("__") and attr not in ALLOWED_DUNDERS:
            self.fail(node, f"the attribute .{attr} is not allowed (interpreter internals)")
        elif attr in FORBIDDEN_ATTRIBUTES:
            self.fail(node, f".{attr} is not allowed: {FORBIDDEN_ATTRIBUTES[attr]}")
        else:
            paths = self.resolve(node)
            if self.check_paths(node, paths):
                # One message per chain: `bpy.ops.script.reload` is reported
                # once, not again for `bpy.ops.script`. Whatever the chain
                # hangs off is still checked.
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if not isinstance(root, ast.Name):
                    self.visit(root)
                return
            self.check_namespace_use(node, paths)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id in ATTRIBUTE_CALLS:
            if len(node.args) < 2:
                self.fail(node, f"{func.id}() needs an object and an attribute name")
            else:
                values = self.check_attribute_name(node, func.id, node.args[1])
                targets = self.resolve(node.args[0])
                if values and targets:
                    self.check_paths(node, {f"{t}.{v}" for t in targets for v in values})
                    if not _literal_lookup(node) and any(sensitive(t) for t in targets):
                        self.fail(node, f"{min(targets)} {NAMESPACE_USE}")
                if _literal_lookup(node):
                    self.check_namespace_use(node, self.resolve(node))
            for arg in node.args:
                self.visit(arg)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        if isinstance(func, ast.Attribute) and func.attr in {"format", "format_map"}:
            receiver = func.value
            if not (isinstance(receiver, ast.Constant) and isinstance(receiver.value, str)):
                self.fail(node, "str.format is only allowed on a string literal; use an f-string")
            elif any("." in field for field in _format_fields(receiver.value)):
                # "{:.2f}" is a format spec and fine; "{0.__class__}" walks attributes.
                self.fail(node, "format fields may not reach into attributes; use an f-string")
        self.generic_visit(node)

    def check_attribute_name(self, node, function, name):
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            values = [name.value]
        elif isinstance(name, ast.Name) and name.id in self.literal_names:
            values = self.literal_names[name.id]
        else:
            self.fail(
                node,
                f"{function}() needs a literal attribute name (or a loop variable over a literal "
                "list of names); computed names can reach interpreter internals",
            )
            return []
        for value in values:
            if value.startswith("__") and value not in ALLOWED_DUNDERS:
                self.fail(node, f"{function}() of {value} is not allowed (interpreter internals)")
            elif value in FORBIDDEN_ATTRIBUTES:
                self.fail(node, f"{function}() of {value} is not allowed: {FORBIDDEN_ATTRIBUTES[value]}")
        return values

    # -- bindings ---------------------------------------------------------------

    def visit_Assign(self, node):
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "expression":
                self.check_driver_expression(node, node.value)
        self.generic_visit(node)

    def check_driver_expression(self, node, value):
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            self.fail(
                node,
                "set driver expressions with a string literal; use driver variables "
                "(driver.variables.new()) for values",
            )
            return
        if re.search(r"__|\bimport\b|\beval\b|\bexec\b|\bopen\b|\bcompile\b", value.value):
            self.fail(node, "driver expressions may only use variables, numbers and math functions")

    def visit_For(self, node):
        self._bind_literal_loop(node.target, node.iter)
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self._bind_literal_loop(node.target, node.iter)
        self.generic_visit(node)

    def _bind_literal_loop(self, target, iterable):
        names = _string_constants(iterable)
        if isinstance(target, ast.Name):
            if names is not None:
                self.literal_names[target.id] = names
            else:
                self.literal_names.pop(target.id, None)
            return
        # for key, value in {"roughness": 0.4}.items()
        if (
            isinstance(target, ast.Tuple)
            and target.elts
            and isinstance(target.elts[0], ast.Name)
            and isinstance(iterable, ast.Call)
            and isinstance(iterable.func, ast.Attribute)
            and iterable.func.attr == "items"
            and isinstance(iterable.func.value, ast.Dict)
        ):
            keys = iterable.func.value.keys
            if keys and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in keys):
                self.literal_names[target.elts[0].id] = [k.value for k in keys]
                return
        for element in ast.walk(target):
            if isinstance(element, ast.Name):
                self.literal_names.pop(element.id, None)


def resolve(node, aliases):
    """Every dotted path an expression can stand for, rooted at an import or alias."""
    if isinstance(node, ast.Name):
        return set(aliases.get(node.id, ()))
    if isinstance(node, ast.Attribute):
        return {f"{path}.{node.attr}" for path in resolve(node.value, aliases)}
    if _literal_lookup(node, {"getattr"}):
        return {f"{path}.{node.args[1].value}" for path in resolve(node.args[0], aliases)}
    if isinstance(node, ast.NamedExpr):
        return resolve(node.value, aliases)
    return set()


def collect_aliases(tree):
    """Name -> every path it is bound to anywhere in the script (flow-insensitive).

    `if c: w = bpy.ops.wm` / `else: w = bpy.ops.mesh` makes `w` both, so
    `w.save_mainfile()` is judged against both, whichever branch runs.
    """
    aliases = {}
    bindings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases.setdefault(alias.asname, set()).add(alias.name)
                else:
                    root = alias.name.split(".")[0]
                    aliases.setdefault(root, set()).add(root)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for alias in node.names:
                if alias.name != "*":
                    aliases.setdefault(alias.asname or alias.name, set()).add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Assign):
            bindings.extend((t.id, node.value) for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            if node.value is not None:
                bindings.append((node.target.id, node.value))
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            bindings.append((node.target.id, node.value))
    changed = True
    while changed:
        changed = False
        for name, value in bindings:
            paths = resolve(value, aliases)
            known = aliases.setdefault(name, set())
            if not paths <= known:
                known |= paths
                changed = True
    return aliases


def _string_constants(node):
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and node.elts:
        if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
            return [e.value for e in node.elts]
    return None


def check(code):
    """Every violation in `code`, in source order. Empty means accepted."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as error:
        return [Violation(error.lineno or 0, f"SyntaxError: {error.msg}")]
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._astra_parent = parent
    checker = _Checker(collect_aliases(tree))
    checker.visit(tree)
    seen, unique = set(), []
    for violation in sorted(checker.violations, key=lambda v: v.line):
        if (violation.line, violation.message) not in seen:
            seen.add((violation.line, violation.message))
            unique.append(violation)
    return unique


def validate(code):
    violations = check(code)
    if violations:
        raise GuardRejected(violations)


def rejection_text(error):
    return (
        "TOOL ERROR: Astra Guard rejected this script before it reached Blender - "
        + str(error)
        + ". Rewrite it with bpy, bmesh, mathutils and plain Python; Astra saves scene files itself."
    )
