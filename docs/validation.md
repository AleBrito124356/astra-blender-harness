# Validation record

Validated on Windows, Python 3.14 and Blender 4.5.9 LTS on 2026-09-09.

- Unit/integration suite exercises phase completion, budgets, read-only phases, approval denial/cancellation, JSON repair, secret redaction, save-file verification, local Host/Origin/auth checks and provider argument adaptation.
- A real stdio MCP fixture verifies the SDK handshake, tool discovery, schemas and image transport.
- **Live Blender integration passed** using the official `blender-mcp==1.9.1` package and its bundled add-on in a disposable Blender 4.5.9 GUI process. A scripted provider drove the actual harness: inspect scene, capture viewport, save checkpoint, create geometry, inspect again and save the final `.blend`. Files were verified on disk. This was not a model API call.
- Edge UI checks passed at 1440 × 1080 and 390 × 844: scene preset, provider switching, complete offline demo, three preview images, manifest download, no horizontal mobile overflow and no JavaScript exceptions.
- The companion lab generated three real 960 × 720 Cycles renders and reopened all three scenes for inspection.
- The supplied CI template targets Python 3.11/3.13 on Windows and Ubuntu; it is not activated in GitHub Actions. The live Blender test is opt-in.

## Repeat the live check

Install the official add-on, start its server in a **disposable** Blender scene, install `blender-mcp`, and create a local MCP configuration with its executable. Set `ASTRA_LIVE_MCP_CONFIG` to that configuration's absolute path, then:

```sh
python -m pytest tests/test_live_blender.py -q
```

This creates a torus and scene copies. It does not require an LLM API key.

## Limits of the evidence

No paid OpenAI, Anthropic, Google or OpenRouter request was made. Provider routing is tested with mocked SDK responses; real account/model permissions and provider-specific behavior still need testing with the user's own key. No aesthetic model benchmark or cross-platform Blender test is claimed. The demo image is an illustration. The live smoke test proves integration and artifact creation, not artistic quality.
