"""The script guard for raw execute_blender_code. Owned by WS5.

INTERFACE PINNED BY THE FOUNDATION. WS6 calls prepare() before asking for
approval and interpret() on the result; WS5 replaces the bodies with local
safe-mode validation plus rewrite hints, the 5.2 API lint, the ax_ prelude
and line instrumentation:

    prepare(code, tier) -> Prepared      raises errors.GuardRejection(text)
    interpret(text, prepared) -> (text, is_error)

The approver always sees Prepared.display_code (the model's code); Blender
receives Prepared.send_code.
"""

from dataclasses import dataclass, field

from . import scripts


@dataclass(frozen=True)
class Prepared:
    """A script ready to send.

    send_code     what Blender runs (prelude and instrumentation included)
    display_code  what the approver reads: the model's own code
    lint_notes    API-drift warnings to show the model with the result
    line_map      {sent line: model line} for mapping error lines back
    """

    send_code: str
    display_code: str
    lint_notes: tuple = ()
    line_map: dict = field(default_factory=dict)


def prepare(code, tier="large"):
    """Check and wrap a model's script. Placeholder: passes it through unchanged."""
    return Prepared(send_code=code, display_code=code)


def interpret(text, prepared):
    """(model-facing text, is_error) for a raw script's result.

    Placeholder: the harness's standard classification (scripts.classify),
    which leaves a success untouched and prefixes an error once.
    """
    return scripts.classify(text)
