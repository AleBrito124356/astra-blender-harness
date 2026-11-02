import json
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


async def test_claude_requests_mark_cache_breakpoints_on_the_wire():
    """The loop resends the conversation every turn; Claude only caches marked prefixes.

    A fake Anthropic endpoint captures the real request LiteLLM sends, so this
    checks the wire format, not just the arguments Astra passes.
    """
    import asyncio
    import socket

    import uvicorn
    from fastapi import FastAPI, Request

    captured = {}
    app = FastAPI()

    @app.post("/v1/messages")
    async def messages(request: Request):
        captured["body"] = await request.json()
        return {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "Phase summary."}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        config = RunConfig(
            prompt="Create a lamp",
            model="anthropic/claude-sonnet-5-5",
            api_key="fixture-key-123456",
            api_base=f"http://127.0.0.1:{port}",
        )
        conversation = [
            {"role": "system", "content": "You are Astra."},
            {"role": "user", "content": "BRIEF: a lamp"},
            {"role": "assistant", "content": "Plan: a ceramic lamp."},
            {"role": "user", "content": "PHASE BUILD: build it."},
        ]
        message, usage = await LiteLLMProvider(config).complete(conversation, [])
        assert message["content"] == "Phase summary."
        body = captured["body"]
        system = body["system"] if isinstance(body["system"], list) else [body["system"]]
        assert system[-1]["cache_control"]["type"] == "ephemeral"
        last = body["messages"][-1]["content"]
        assert isinstance(last, list) and last[-1]["cache_control"]["type"] == "ephemeral"
        earlier = json.dumps(body["messages"][:-1])
        assert "cache_control" not in earlier, "two breakpoints: the system prompt and the newest message"

        config.prompt_cache = False
        await LiteLLMProvider(config).complete(conversation, [])
        assert "cache_control" not in json.dumps(captured["body"])
    finally:
        server.should_exit = True
        await task


async def test_other_providers_get_no_cache_markers(monkeypatch):
    import litellm

    captured = {}

    async def complete(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(finish_reason="stop", message=SimpleNamespace(model_dump=lambda **_: {}))
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm, "acompletion", complete)
    await LiteLLMProvider(RunConfig(prompt="Create a lamp", model="openai/gpt-6-astra")).complete([], [])
    assert "cache_control_injection_points" not in captured
