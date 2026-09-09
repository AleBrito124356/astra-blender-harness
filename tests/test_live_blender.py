"""Opt-in real Blender MCP smoke test. Modifies the connected disposable scene."""

import json
import os
from pathlib import Path

import pytest

from astra_blender.config import MCPConfig, RunConfig
from astra_blender.engine import Run, execute


@pytest.mark.skipif(
    not os.environ.get("ASTRA_LIVE_MCP_CONFIG"), reason="Requires a disposable live Blender MCP session"
)
async def test_live_blender_scene_copy_and_viewport(tmp_path):
    class Scripted:
        turn = 0

        async def complete(self, messages, tools):
            self.turn += 1
            if self.turn == 2:
                return {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "smoke",
                            "type": "function",
                            "function": {
                                "name": "execute_blender_code",
                                "arguments": json.dumps(
                                    {
                                        "code": "import bpy\nbpy.ops.mesh.primitive_torus_add(location=(0,0,2))\nbpy.context.object.name='ASTRA_LIVE_TEST'"
                                    }
                                ),
                            },
                        }
                    ],
                }, {"total_tokens": 0}
            return {"role": "assistant", "content": "Scripted test phase finished; no LLM called."}, {
                "total_tokens": 0
            }

    config = MCPConfig.model_validate_json(
        Path(os.environ["ASTRA_LIVE_MCP_CONFIG"]).read_text(encoding="utf-8")
    )
    run = Run(
        RunConfig(
            prompt="Create a torus for the Astra integration test.",
            model="test/scripted",
            auto_approve=True,
            quality="draft",
        ),
        tmp_path,
    )
    await execute(run, config, provider=Scripted())
    assert run.status == "completed"
    assert (run.directory / "scene.blend").is_file()
    assert list(run.directory.glob("viewport-*.png"))
