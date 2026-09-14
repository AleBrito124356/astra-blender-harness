import litellm
import pytest

from astra_blender.config import RunConfig
from astra_blender.engine import provider_failure
from astra_blender.provider import LiteLLMProvider


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
