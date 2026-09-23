"""Provider adaptation lives here; the engine knows only messages and tool calls."""

import json

from .config import RunConfig


class LiteLLMProvider:
    def __init__(self, config: RunConfig):
        self.config = config

    async def complete(self, messages, tools):
        import litellm

        # No callbacks, proxy, disk cache or prompt logging are configured by Astra.
        kwargs = dict(
            model=self.config.model,
            messages=messages,
            max_tokens=self.config.max_output_tokens,
            timeout=self.config.request_timeout,
            # Retries are handled here, not by LiteLLM, so a timeout is never one
            # of them: a generation that ran out of time does not get faster on a
            # second full attempt, and the provider has already billed the first.
            num_retries=0,
        )
        if self.config.api_key.get_secret_value():
            kwargs["api_key"] = self.config.api_key.get_secret_value()
        if self.config.api_base:
            kwargs["api_base"] = self.config.api_base
        if self.config.tool_mode == "native":
            kwargs["tools"] = tools
        try:
            response = await litellm.acompletion(**kwargs)
        except (litellm.APIConnectionError, litellm.InternalServerError):
            # One retry for a transient failure, as documented.
            response = await litellm.acompletion(**kwargs)
        if not response.choices or response.choices[0].finish_reason in {"length", "content_filter"}:
            raise RuntimeError("Provider response was truncated or filtered; phase is incomplete")
        message = response.choices[0].message.model_dump(exclude_none=True)
        usage = response.usage.model_dump() if response.usage else {}
        # Preserve provider-specific tool call metadata (e.g. Gemini thought signatures).
        return message, usage


def parse_json_action(content: str):
    content = content.strip()
    if content.startswith("```") and content.endswith("```"):
        content = "\n".join(content.splitlines()[1:-1])
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("Expected one JSON object")
    if "tool" in data and isinstance(data.get("arguments"), dict):
        if not isinstance(data["tool"], str):
            raise ValueError("tool must be a string")
        return data["tool"], data["arguments"]
    if isinstance(data.get("done"), str):
        return None, data["done"]
    raise ValueError('Return {"tool":"name","arguments":{...}} or {"done":"summary"}')
