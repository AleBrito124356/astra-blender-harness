"""Astra Guard: static checks on model-written Blender Python.

Model-written code reaches Blender through ``execute_blender_code``. Tool
output, asset names and reference images flow into the model's context, and a
prompt-injected model can be talked into writing hostile code that a person
approves without reading every line. The guard rejects the constructs such
code needs before a byte reaches Blender:

* imports outside a short allowlist (no ``os``, ``sys``, ``subprocess``,
  ``socket``, ``importlib``, ``pickle``, ``ctypes`` ...);
* interpreter escapes: ``eval``/``exec``/``compile``/``__import__``/``open``,
  dunder attributes and names, frame and traceback internals, computed
  ``getattr``/``setattr`` names, ``str.format`` on strings that reach into
  attributes;
* code that outlives the call or loads code: ``bpy.app.handlers``/``timers``,
  ``register_class``, text datablocks, linked or appended .blend libraries,
  script/text/preferences/extensions operators, auto-run of embedded scripts;
* scene-file operators. Astra saves verified copies itself; the model opening,
  reverting or overwriting files would break the checkpoints and could
  overwrite your original ``.blend``.

It is a guard, not a sandbox: Blender's Python is not confined, and code the
guard accepts runs with your user's permissions. Approvals and a disposable
Blender session remain the real boundary. Astra's own reviewed scripts do not
pass through it; they never contain model text.
"""

import ast
import re
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
        "string",
        "re",
        "enum",
        "dataclasses",
        "typing",
        "decimal",
        "fractions",
        "textwrap",
        "copy",
        "heapq",
        "bisect",
        "array",
        "time",
        "numpy",
    }
)

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

ALLOWED_DUNDERS = frozenset({"__name__", "__doc__"})

# Blocked wherever they appear, because the receiver cannot always be resolved
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

FORBIDDEN_PATHS = (
    ("bpy.ops.script", "script operators run Python"),
    ("bpy.ops.text", "text operators run text datablocks"),
    ("bpy.ops.preferences", "preferences operators install and enable add-ons"),
    ("bpy.ops.extensions", "extension operators install packages"),
    ("bpy.ops.console", "the console runs arbitrary Python"),
    ("bpy.ops.wm.append", "appending from another .blend can bring its code"),
    ("bpy.ops.wm.link", "linking another .blend can bring its code"),
    ("bpy.ops.wm.lib_relocate", "relocating libraries loads other .blend files"),
    ("bpy.ops.wm.lib_reload", "reloading libraries loads other .blend files"),
    ("bpy.ops.wm.save_mainfile", "Astra saves verified scene copies itself; never overwrite the open file"),
    ("bpy.ops.wm.save_as_mainfile", "Astra saves verified scene copies itself"),
    ("bpy.ops.wm.open_mainfile", "opening another file would replace the scene Astra is tracking"),
    ("bpy.ops.wm.revert_mainfile", "reverting would discard the scene Astra is tracking"),
    ("bpy.ops.wm.recover_last_session", "recovering would replace the scene Astra is tracking"),
    ("bpy.ops.wm.recover_auto_save", "recovering would replace the scene Astra is tracking"),
    ("bpy.ops.wm.read_homefile", "reading the startup file would replace the scene"),
    ("bpy.ops.wm.read_factory_settings", "factory settings would replace the scene"),
    ("bpy.ops.wm.save_homefile", "overwrites your startup file"),
    ("bpy.ops.wm.save_userpref", "overwrites your preferences"),
    ("bpy.ops.wm.url_open", "opens a URL"),
    ("bpy.ops.wm.path_open", "opens a path with the system handler"),
    ("bpy.ops.wm.quit_blender", "would close Blender"),
    ("bpy.ops.render.play_rendered_anim", "launches an external player"),
    ("numpy.load", "numpy.load can unpickle files"),
    ("numpy.save", "writes arbitrary files"),
    ("numpy.savez", "writes arbitrary files"),
    ("numpy.savez_compressed", "writes arbitrary files"),
    ("numpy.savetxt", "writes arbitrary files"),
    ("numpy.loadtxt", "reads arbitrary files"),
    ("numpy.genfromtxt", "reads arbitrary files"),
    ("numpy.fromfile", "reads arbitrary files"),
    ("numpy.fromregex", "reads arbitrary files"),
    ("numpy.memmap", "maps arbitrary files"),
    ("numpy.lib", "numpy.lib contains file and format helpers"),
    ("numpy.ctypeslib", "loads native libraries"),
    ("numpy.f2py", "compiles native code"),
    ("numpy.distutils", "builds native code"),
    ("numpy.testing", "test helpers spawn processes"),
    ("numpy.DataSource", "opens files and URLs"),
    ("time.sleep", "sleeping blocks Blender; there is nothing to wait for"),
)

# A format string that reaches into attributes or items: "{0.__class__}".
FORMAT_REACH = re.compile(r"\{[^{}]*[.\[]")


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


class _Checker(ast.NodeVisitor):
    def __init__(self):
        self.violations = []
        # Local name -> dotted path it stands for: `import numpy as np`,
        # `from bpy import ops`, `d = bpy.data`.
        self.aliases = {}
        # Names that only ever hold string constants, from `for name in ("a", "b")`.
        self.literal_names = {}

    def fail(self, node, message):
        self.violations.append(Violation(getattr(node, "lineno", 0), message))

    # -- resolution ---------------------------------------------------------

    def resolve(self, node):
        """Dotted path for `a.b.c` rooted at an import or alias, else None."""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Call) and self._literal_getattr(node):
            inner = self.resolve(node.args[0])
            if inner is None:
                return None
            parts.append(node.args[1].value)
            return ".".join([inner, *reversed(parts)])
        if not isinstance(node, ast.Name) or node.id not in self.aliases:
            return None
        return ".".join([self.aliases[node.id], *reversed(parts)])

    def _literal_getattr(self, node):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        )

    def check_path(self, node, path):
        if path is None:
            return False
        for prefix, reason in FORBIDDEN_PATHS:
            if _matches(path, prefix):
                self.fail(node, f"{path} is not allowed: {reason}")
                return True
        return False

    # -- imports --------------------------------------------------------------

    def check_module(self, node, module):
        root = module.split(".")[0]
        if root not in ALLOWED_MODULES:
            allowed = ", ".join(sorted(ALLOWED_MODULES))
            self.fail(node, f"import of '{module}' is not allowed. Allowed modules: {allowed}")
            return False
        self.check_path(node, module)
        return True

    def visit_Import(self, node):
        for alias in node.names:
            if self.check_module(node, alias.name):
                if alias.asname:
                    self.aliases[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    self.aliases[root] = root

    def visit_ImportFrom(self, node):
        if node.level:
            self.fail(node, "relative imports are not allowed")
            return
        module = node.module or ""
        if not self.check_module(node, module):
            return
        for alias in node.names:
            if alias.name == "*":
                self.fail(node, f"'from {module} import *' hides what is imported; name it")
                continue
            path = f"{module}.{alias.name}"
            if alias.name.startswith("__"):
                self.fail(node, f"importing {path} is not allowed")
                continue
            if alias.name in FORBIDDEN_ATTRIBUTES:
                self.fail(node, f"importing {path} is not allowed: {FORBIDDEN_ATTRIBUTES[alias.name]}")
                continue
            self.check_path(node, path)
            self.aliases[alias.asname or alias.name] = path

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

    def visit_Attribute(self, node):
        attr = node.attr
        if attr.startswith("__") and attr not in ALLOWED_DUNDERS:
            self.fail(node, f"the attribute .{attr} is not allowed (interpreter internals)")
        elif attr in FORBIDDEN_ATTRIBUTES:
            self.fail(node, f".{attr} is not allowed: {FORBIDDEN_ATTRIBUTES[attr]}")
        elif self.check_path(node, self.resolve(node)):
            # One message per chain: `bpy.ops.script.reload` is reported once,
            # not again for `bpy.ops.script`. Whatever the chain hangs off is
            # still checked.
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            self.visit(root)
            return
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id in ATTRIBUTE_CALLS:
            if len(node.args) < 2:
                self.fail(node, f"{func.id}() needs an object and an attribute name")
            else:
                self.check_attribute_name(node, func.id, node.args[1])
                if func.id == "getattr" and self._literal_getattr(node):
                    self.check_path(node, self.resolve(node))
            for arg in node.args:
                self.visit(arg)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        if isinstance(func, ast.Attribute) and func.attr in {"format", "format_map"}:
            receiver = func.value
            if not isinstance(receiver, ast.Constant):
                self.fail(node, "str.format is only allowed on a string literal; use an f-string")
            elif isinstance(receiver.value, str) and FORMAT_REACH.search(receiver.value):
                self.fail(node, "format fields may not reach into attributes or items; use an f-string")
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
            return
        for value in values:
            if value.startswith("__") and value not in ALLOWED_DUNDERS:
                self.fail(node, f"{function}() of {value} is not allowed (interpreter internals)")
            elif value in FORBIDDEN_ATTRIBUTES:
                self.fail(node, f"{function}() of {value} is not allowed: {FORBIDDEN_ATTRIBUTES[value]}")

    # -- bindings ---------------------------------------------------------------

    def visit_Assign(self, node):
        path = self.resolve(node.value) if isinstance(node.value, (ast.Attribute, ast.Name)) else None
        for target in node.targets:
            if isinstance(target, ast.Name):
                if path is not None:
                    self.aliases[target.id] = path
                else:
                    self.aliases.pop(target.id, None)
            if isinstance(target, ast.Attribute) and target.attr == "expression":
                self.check_driver_expression(node, node.value)
        self.generic_visit(node)

    def check_driver_expression(self, node, value):
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            self.fail(node, "driver expressions must be string literals")
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
    checker = _Checker()
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
