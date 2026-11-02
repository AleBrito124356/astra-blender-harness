"""Shared fixtures.

`blender_config` selects a real Blender for the integration tests:

1. ASTRA_LIVE_MCP_CONFIG: a JSON connection file for a disposable Blender (any
   transport, including a GUI Blender running the add-on).
2. Otherwise a headless Blender Astra can start itself: ASTRA_BLENDER/BLENDER
   (a Blender executable or a Python with the bpy wheel), `blender` on PATH,
   or bpy installed in this interpreter.

Without either, those tests are skipped; nothing else needs Blender.
"""

import os
from pathlib import Path

import pytest

from astra_blender.backends import BlenderUnavailable, find_blender
from astra_blender.config import MCPConfig


def live_config():
    path = os.environ.get("ASTRA_LIVE_MCP_CONFIG")
    if path:
        return MCPConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))
    try:
        find_blender()
    except BlenderUnavailable:
        return None
    return MCPConfig(transport="headless", preview_engine=os.environ.get("ASTRA_PREVIEW_ENGINE", "auto"))


@pytest.fixture
def blender_config():
    config = live_config()
    if config is None:
        pytest.skip("Needs a disposable Blender: set ASTRA_BLENDER to a bpy Python or Blender executable")
    return config
