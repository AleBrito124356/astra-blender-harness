from types import SimpleNamespace

import pytest

from astra_blender.config import RunConfig
from astra_blender.provider import LiteLLMProvider


@pytest.mark.parametrize(
    "model", ["openai/gpt-6-astra", "anthropic/claude-opus-5", "gemini/gemini-2.5-flash", "ollama_chat/local"]
)
async def test_provider_arguments_and_native_metadata(monkeypatch, model):
    import litellm

    captured = {}
    message = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "one",
                "type": "function",
                "function": {"name": "get_scene_info", "arguments": "{}"},
                "provider_specific_fields": {"thought_signature": "fixture"},
            }
        ],
    }

    async def complete(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls", message=SimpleNamespace(model_dump=lambda **_: message)
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 42}),
        )

    monkeypatch.setattr(litellm, "acompletion", complete)
    config = RunConfig(
        prompt="Create a lamp", model=model, api_key="fixture-key", api_base="http://localhost:9999"
    )
    result, usage = await LiteLLMProvider(config).complete(
        [{"role": "user", "content": "hello"}], [{"fixture": True}]
    )
    assert captured["model"] == model
    assert captured["api_key"] == "fixture-key"
    assert captured["tools"] == [{"fixture": True}]
    assert result == message
    assert usage["total_tokens"] == 42


async def test_json_mode_omits_native_tools(monkeypatch):
    import litellm

    captured = {}

    async def complete(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        model_dump=lambda **_: {"role": "assistant", "content": '{"done":"ok"}'}
                    ),
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm, "acompletion", complete)
    await LiteLLMProvider(RunConfig(prompt="Create a lamp", tool_mode="json")).complete([], [])
    assert "tools" not in captured


async def test_truncated_response_is_not_a_finished_phase(monkeypatch):
    import litellm

    async def complete(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])

    monkeypatch.setattr(litellm, "acompletion", complete)
    with pytest.raises(RuntimeError):
        await LiteLLMProvider(RunConfig(prompt="Create a lamp")).complete([], [])
