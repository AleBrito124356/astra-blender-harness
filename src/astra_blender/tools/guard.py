"""WS5 Guard: tools that help a model write correct raw Blender Python.

Foundation state: empty. WS5 registers astra_api_lookup here (readonly;
medium and large tiers). The script guard itself is not a tool: it plugs into
the engine through astra_blender.guard.prepare / interpret.
"""

TOOLS = []

PROMPT_CARDS = {}

RECIPES = []
