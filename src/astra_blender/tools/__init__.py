"""Trusted tool modules. The registry imports each one listed here.

Every module exports TOOLS (a list of registry.ToolSpec), PROMPT_CARDS
({tier: text}) and RECIPES (a list of registry.Recipe). Each workstream owns
one module and fills it; this list is complete for 0.4, so nobody edits it:

    sight    WS1  perception: facts, look check, motion facts, plan view
    toolkit  WS2  make and shoot: build, material, place, light, camera, render
    motion   WS3  animation presets, explode, split and assemble parts
    judge    WS4  scene spec and checks (harness tools)
    guard    WS5  API lookup

tools/contract.json pins the planned 0.4 tool names and the arguments that
fixes and recipes rely on.
"""

TOOL_MODULES = (
    "astra_blender.tools.sight",
    "astra_blender.tools.toolkit",
    "astra_blender.tools.motion",
    "astra_blender.tools.judge",
    "astra_blender.tools.guard",
)
