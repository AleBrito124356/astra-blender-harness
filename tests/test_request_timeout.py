import asyncio
import time

import litellm
import pytest

from astra_blender.config import RunConfig
from astra_blender.engine import provider_failure
from astra_blender.provider import LiteLLMProvider

TOOLS = [
    {
        "type": "function",
        "function": {"name": "noop", "description": "x", "parameters": {"type": "object", "properties": {}}},
    }
]


class Recorder:
    """Stands in for litellm.acompletion and records every attempt."""

    def __init__(self, failures=()):
        self.calls = []
        self.failures = list(failures)

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)

        class Message:
            def model_dump(self, exclude_none=False):
                return {"role": "assistant", "content": "ok"}

        class Choice:
            finish_reason = "stop"
            message = Message()

        class Usage:
            def model_dump(self):
                return {"total_tokens": 7}

        class Response:
            choices = [Choice()]
            usage = Usage()

        return Response()


def provider(**config):
    return LiteLLMProvider(RunConfig(prompt="Create a lamp", **config))


async def test_the_timeout_is_a_setting_not_a_constant(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(litellm, "acompletion", recorder)
    await provider(request_timeout=900).complete([], [])
    assert recorder.calls[0]["timeout"] == 900
    # LiteLLM must not retry on its own; retries are decided here.
    assert recorder.calls[0]["num_retries"] == 0


async def test_a_timed_out_turn_is_not_retried(monkeypatch):
    # A generation that ran out of time does not get faster on a second full
    # attempt, and the provider has already billed the first.
    recorder = Recorder(failures=[litellm.Timeout("too slow", model="m", llm_provider="p")])
    monkeypatch.setattr(litellm, "acompletion", recorder)
    with pytest.raises(litellm.Timeout):
        await provider().complete([], [])
    assert len(recorder.calls) == 1


async def test_a_turn_with_no_answer_is_one_request_on_a_real_socket():
    # The test above replaces LiteLLM, so it cannot see the OpenAI client under
    # it, which repeats a timed-out request by itself. That hid the retries for
    # two releases: a 300 s limit on NVIDIA NIM took 15 silent minutes.
    attempts = 0
    release = asyncio.Event()

    async def silent(reader, writer):
        nonlocal attempts
        attempts += 1
        await reader.read(65536)
        await release.wait()
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = RunConfig(
        prompt="Create a lamp",
        model="nvidia_nim/vendor/model",
        api_key="not-a-real-key",
        api_base=f"http://127.0.0.1:{port}/v1",
    ).model_copy(update={"request_timeout": 1})
    started = time.perf_counter()
    try:
        with pytest.raises(litellm.Timeout):
            await LiteLLMProvider(config).complete([{"role": "user", "content": "hi"}], TOOLS)
    finally:
        release.set()
        server.close()
    assert attempts == 1
    assert time.perf_counter() - started < 2.5


async def test_the_limit_covers_the_whole_turn_not_each_attempt(monkeypatch):
    # A transient failure may be retried, but the turn keeps one deadline.
    async def slow_then_broken(**kwargs):
        await asyncio.sleep(0.6)
        raise litellm.InternalServerError("busy", model="m", llm_provider="p")

    monkeypatch.setattr(litellm, "acompletion", slow_then_broken)
    config = RunConfig(prompt="Create a lamp").model_copy(update={"request_timeout": 1})
    started = time.perf_counter()
    with pytest.raises(litellm.Timeout):
        await LiteLLMProvider(config).complete([], [])
    assert time.perf_counter() - started < 1.15


async def test_a_turn_timeout_is_not_mistaken_for_the_run_deadline(monkeypatch):
    # The engine reads a bare TimeoutError as "the run passed its time limit".
    async def never(**kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(litellm, "acompletion", never)
    config = RunConfig(prompt="Create a lamp").model_copy(update={"request_timeout": 1})
    with pytest.raises(litellm.Timeout) as caught:
        await LiteLLMProvider(config).complete([], [])
    assert not isinstance(caught.value, TimeoutError)
    assert "Model response timeout" in provider_failure(caught.value)


async def test_a_transient_failure_is_retried_once(monkeypatch):
    recorder = Recorder(failures=[litellm.APIConnectionError("dropped", model="m", llm_provider="p")])
    monkeypatch.setattr(litellm, "acompletion", recorder)
    message, usage = await provider().complete([], [])
    assert len(recorder.calls) == 2
    assert usage["total_tokens"] == 7 and message["content"] == "ok"


def test_default_and_bounds():
    assert RunConfig(prompt="Create a lamp").request_timeout == 300
    for bad in (29, 3601):
        with pytest.raises(ValueError):
            RunConfig(prompt="Create a lamp", request_timeout=bad)


def test_a_timeout_says_which_setting_to_raise():
    message = provider_failure(litellm.Timeout("too slow", model="m", llm_provider="p"))
    assert "did not answer within the per-turn limit" in message
    assert "Model response timeout" in message
    assert "not retry" in message
