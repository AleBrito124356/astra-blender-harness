"""Actions from a JSON-mode reply. Owned by WS5.

INTERFACE PINNED BY THE FOUNDATION. WS6's loop calls parse() for models
without native tool calls; WS5 replaces the body with the lenient parser
(think blocks, prose around JSON, several actions, fenced Python):

    parse(content, allowed=None, max_actions=8) -> list[Action]
        raises errors.ActionError(text) - text is the repair request

The body below is exactly provider.parse_json_action's 0.3.2 contract: one
whole-content JSON object, {"tool", "arguments"} or {"done"}.
"""

from dataclasses import dataclass, field

from .errors import ActionError
from .provider import parse_json_action


@dataclass(frozen=True)
class Action:
    """One parsed action: a tool call, or done with its phase summary."""

    tool: str | None
    arguments: dict = field(default_factory=dict)
    done: str | None = None

    @property
    def is_done(self):
        return self.tool is None


def parse(content, allowed=None, max_actions=8):
    """The actions in one JSON-mode reply.

    allowed (tool names) and max_actions are part of the pinned signature;
    the 0.3.2 body returns one action and leaves unknown names to the
    engine's own 'tool is unavailable' answer.
    """
    try:
        tool, arguments = parse_json_action(content or "")
    except (ValueError, TypeError) as error:
        raise ActionError(str(error)) from error
    if tool is None:
        return [Action(tool=None, done=arguments)]
    return [Action(tool=tool, arguments=arguments)][: max(1, int(max_actions))]
