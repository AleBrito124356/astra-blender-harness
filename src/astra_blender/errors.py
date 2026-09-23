"""Exceptions shared by the tool registry, the script guard and the model I/O layer.

Each carries text written for the model or the person, never a provider body:
the engine turns them into a "TOOL ERROR: ..." message the model can act on,
instead of a failed run.
"""

TOOL_ERROR = "TOOL ERROR: "


def tool_error_text(text):
    """The model-visible form of a failure: always starts with TOOL ERROR."""
    text = str(text)
    return text if text.startswith(TOOL_ERROR) else TOOL_ERROR + text


class ToolFailure(Exception):
    """A tool call that cannot run or whose result cannot be used.

    `text` is exactly what the model sees. Raised before anything reaches
    Blender (non-finite numbers, a harness-side check) or after (no result
    marker, a safe-mode rejection, an error inside the script).
    """

    def __init__(self, text):
        self.text = tool_error_text(text)
        super().__init__(self.text)


class GuardRejection(ToolFailure):
    """The script guard refused raw Python before approval (WS5 guard.prepare).

    Nothing was sent to Blender and no approval was spent.
    """


class TruncatedReply(RuntimeError):
    """The provider stopped at its output limit (finish_reason=length).

    `chars` is how much content arrived, so the loop can ask for a shorter
    answer instead of failing the run. A RuntimeError, as the 0.3.2 provider
    raised, so existing handlers keep working.
    """

    def __init__(self, chars=0, message=None):
        self.chars = int(chars or 0)
        super().__init__(
            message or f"Provider response was truncated at its output limit after {self.chars} characters"
        )


class ActionError(ValueError):
    """A JSON-mode reply held no usable action. `text` is the repair request.

    A ValueError, as provider.parse_json_action raised, so the loop's repair
    path catches it unchanged.
    """

    def __init__(self, text):
        self.text = str(text)
        super().__init__(self.text)
