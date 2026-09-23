"""WS4 Judge: the checkable scene spec and the measured checks.

Foundation state: empty. WS4 registers two harness tools here -
astra_set_spec and astra_check, both kind='harness' and readonly - whose
handlers read Blender only through ctx.call('astra_scene_facts' | ...), so
they never need approval. Their arguments are pinned in tools/contract.json.
"""

TOOLS = []

PROMPT_CARDS = {}

RECIPES = []
