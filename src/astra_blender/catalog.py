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
    # NIM serves dozens of chat models but registers only rerank entries in the
    # LiteLLM catalog, so its list arrives empty here and fills in from the
    # provider's own /models endpoint.
    ("nvidia_nim", "NVIDIA NIM", "nvidia_nim"),
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


# Live discovery ------------------------------------------------------------
#
# A static catalog cannot keep up with gateways. NVIDIA NIM, for instance, ships
# three rerank entries in LiteLLM's catalog while serving dozens of chat models
# under identifiers like "deepseek-ai/deepseek-v4-pro-0813". Nearly every such
# service exposes the OpenAI-compatible GET /models, so ask the provider itself
# instead of guessing, and hand back identifiers LiteLLM can actually route.

DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "nvidia_nim": "https://integrate.api.nvidia.com/v1",
    "ollama_chat": "http://localhost:11434/v1",
}

# Providers LiteLLM routes from the identifier prefix alone. Anything else needs
# the OpenAI-compatible route, which pairs "openai/<id>" with an explicit base.
NATIVE_PREFIXES = {"openai", "anthropic", "gemini", "deepseek", "openrouter", "nvidia_nim", "ollama_chat"}


def routable(provider: str, identifier: str) -> str:
    """The identifier LiteLLM can route, given the provider the user picked."""
    if provider in NATIVE_PREFIXES:
        return f"{provider}/{identifier}"
    return f"openai/{identifier}"


NOT_ROUTABLE = (
    "Model '{model}' has no provider LiteLLM recognises, so the run would fail on its first model "
    "turn - after Blender has already been read and checkpointed. Identifiers copied from a "
    "provider's own page usually need the provider in front: '{provider}/{model}' for NVIDIA NIM, "
    "'openrouter/{model}' for OpenRouter. Setting the API base URL also works, because LiteLLM "
    "then infers the provider from it. Use the refresh button beside the model list to fetch exact "
    "identifiers from your provider."
)


def routing_problem(model: str, api_base: str | None = None):
    """Why LiteLLM cannot route this identifier, or None when it can.

    Pasting what a provider's page shows - "moonshotai/kimi-k3",
    "deepseek-ai/deepseek-v4-pro-0813" - is the natural thing to do and the
    most common way a run dies on its first model turn. An API base is enough
    on its own: LiteLLM infers the provider from the URL.
    """
    import litellm

    if api_base or not model:
        return None
    try:
        litellm.get_llm_provider(model=model)
    except Exception:
        return NOT_ROUTABLE.format(model=model, provider="nvidia_nim")
    return None


async def discover(provider: str, api_base: str | None, api_key: str):
    """List the models a provider actually serves, via its /models endpoint."""
    import httpx

    base = (api_base or DEFAULT_BASES.get(provider) or "").rstrip("/")
    if not base:
        raise ValueError("Set the API base URL first; this provider has no known default.")
    headers = {"Accept": "application/json"}
    if api_key:
        # Anthropic authenticates with its own header rather than a bearer token.
        if provider == "anthropic":
            headers |= {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(base + "/models", headers=headers)
    except httpx.HTTPError:
        raise ValueError("Could not reach that endpoint. Check the API base URL and your network.")
    if response.status_code in {401, 403}:
        raise ValueError("The provider rejected the key. Check it, or that it may list models.")
    if response.status_code >= 400:
        raise ValueError(f"The provider returned HTTP {response.status_code} for /models.")
    try:
        payload = response.json()
    except ValueError:
        raise ValueError("That endpoint did not return JSON. It may not be OpenAI-compatible.")
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Unexpected /models response shape; no model list found.")
    found, seen = [], set()
    for row in rows:
        identifier = row.get("id") if isinstance(row, dict) else row
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            continue
        seen.add(identifier)
        known = describe(routable(provider, identifier)) or {}
        found.append(
            {
                "id": routable(provider, identifier),
                "raw": identifier,
                "tools": bool(known.get("tools")),
                "vision": bool(known.get("vision")),
                "context": known.get("context") or 0,
                "live": True,
            }
        )
    if not found:
        raise ValueError("The provider listed no models.")
    return sorted(found, key=lambda m: m["id"])
