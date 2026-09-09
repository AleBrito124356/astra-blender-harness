# Contributing

Use Python 3.11+, install `.[dev]`, and run pytest, Ruff and the wheel build before opening a pull request. Keep Blender mutations sequential and preserve the distinction between completion, failure and budget exhaustion.

Provider support needs evidence: include the exact model/provider ID, native or JSON mode, vision setting and a redacted minimal reproduction. Never upload API keys, private scenes or unreviewed run traces. Add adapters in `provider.py`; keep provider-specific details out of the engine. Changes to the MCP bridge should exercise an actual stdio fixture.

A quality improvement should include the same scene brief before and after, comparable budgets and render settings, and honest failure counts. See Blender Quality Lab for a rubric. Visual polish alone is not a substitute for working tool calls and verified artifacts.
