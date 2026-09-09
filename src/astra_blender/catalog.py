"""Build the model picker from the installed LiteLLM catalog.

LiteLLM stores identifiers inconsistently: OpenAI and Anthropic entries are bare
("gpt-5.6-terra"), Gemini and DeepSeek are mixed, OpenRouter is always prefixed.
Callers need the prefixed form, because a bare identifier makes LiteLLM raise
"LLM Provider NOT provided". Everything here normalises to "<provider>/<model>".
"""

# Providers offered in the studio, in display order. The value is the LiteLLM
# provider key in model_cost; ollama_chat borrows the ollama catalog because
# only the non-chat variant carries pricing metadata.
PROVIDERS = [
    ("openai", "OpenAI", "openai"),
    ("anthropic", "Anthropic", "anthropic"),
    ("gemini", "Google Gemini", "gemini"),
    ("deepseek", "DeepSeek", "deepseek"),
    ("openrouter", "OpenRouter", "openrouter"),
    ("ollama_chat", "Ollama · local", "ollama"),
]

# Fine-tune identifiers and non-model helper entries are not usable as a model ID.
_SKIP_PREFIXES = ("ft:",)
_SKIP_EXACT = {"openai/container"}


def _entries(provider: str, source: str):
    import litellm

    seen = {}
    for key, meta in litellm.model_cost.items():
        if meta.get("litellm_provider") != source or meta.get("mode") != "chat":
            continue
        if key in _SKIP_EXACT or key.startswith(_SKIP_PREFIXES):
            continue
        bare = key[len(source) + 1 :] if key.startswith(source + "/") else key
        identifier = f"{provider}/{bare}"
        if identifier in seen:
            continue
        seen[identifier] = {
            "id": identifier,
            "tools": bool(meta.get("supports_function_calling")),
            "vision": bool(meta.get("supports_vision")),
            "context": meta.get("max_input_tokens") or 0,
        }
    return sorted(seen.values(), key=lambda m: m["id"])


def models():
    """Return the chat models per provider, each already usable as a model ID."""
    groups = []
    for provider, label, source in PROVIDERS:
        groups.append({"provider": provider, "label": label, "models": _entries(provider, source)})
    groups.append({"provider": "custom", "label": "Custom / compatible API", "models": []})
    return groups


def describe(identifier: str):
    """Capabilities for one identifier, or None when LiteLLM does not know it."""
    for group in models():
        for model in group["models"]:
            if model["id"] == identifier:
                return model
    return None
