import pytest
from fastapi.testclient import TestClient

from astra_blender import catalog
from astra_blender.engine import provider_failure
from astra_blender.server import create_app


@pytest.mark.parametrize(
    "model",
    [
        # Exactly what a provider's own page shows, and what people paste.
        "moonshotai/kimi-k3",
        "deepseek-ai/deepseek-v4-pro-0813",
        "01-ai/yi-large",
        "qwen/qwen3-coder-480b-a35b-instruct",
    ],
)
def test_identifiers_copied_from_a_provider_page_are_refused_with_a_fix(model):
    problem = catalog.routing_problem(model)
    assert problem is not None
    assert model in problem
    assert f"nvidia_nim/{model}" in problem
    # An API base is enough on its own: LiteLLM infers the provider from it.
    assert catalog.routing_problem(model, "https://integrate.api.nvidia.com/v1") is None


@pytest.mark.parametrize(
    "model", ["nvidia_nim/moonshotai/kimi-k3", "openai/gpt-6-astra", "deepseek/deepseek-v4-pro"]
)
def test_routable_identifiers_pass(model):
    assert catalog.routing_problem(model) is None


def test_a_vendor_prefix_that_is_also_a_litellm_provider_is_not_caught():
    """The guard's known limit, recorded rather than implied.

    Of the 81 identifiers NVIDIA NIM served on 2026-09-14, 72 are rejected
    here with the fix spelled out; nine start with a vendor name that is also
    a LiteLLM provider ("meta/", "databricks/") and route there instead. Those
    reach the wrong provider and fail with its own 401/403, which
    provider_failure now repeats verbatim.
    """
    assert catalog.routing_problem("meta/llama-3.2-90b-vision-instruct") is None


def test_a_run_is_refused_before_touching_blender(tmp_path, monkeypatch):
    started = []

    async def execute(run, *args, **kwargs):
        started.append(run.config.model)
        run.status = "completed"

    monkeypatch.setattr("astra_blender.server.execute", execute)
    with TestClient(create_app(output=tmp_path), base_url="http://127.0.0.1") as client:
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        body = {"prompt": "Animate the car", "model": "moonshotai/kimi-k3", "api_key": "nvapi-test"}
        response = client.post("/api/runs", headers=headers, json=body)
        assert response.status_code == 400
        assert "nvidia_nim/moonshotai/kimi-k3" in response.json()["detail"]
        assert started == [], "no run directory or Blender read should happen"
        body["model"] = "nvidia_nim/moonshotai/kimi-k3"
        assert client.post("/api/runs", headers=headers, json=body).status_code == 200


class Retired(Exception):
    status_code = 410
    message = (
        "litellm.APIError: APIError: Nvidia_nimException - Error code: 410 - {'title': 'Gone', "
        "'detail': \"The model 'deepseek-ai/deepseek-v4-pro-0813' has reached its end of life on "
        '2026-09-14T08:00:00Z and is no longer available."}'
    )


class Rejected(Exception):
    status_code = 403
    message = "litellm.APIError: Nvidia_nimException - Error code: 403 - {'detail': 'Authorization failed'}"


def test_failure_messages_name_the_status_without_repeating_the_body():
    """The status is actionable; the body is not ours to surface.

    A provider body can echo credentials or the request payload, so it stays
    in error.log. What reaches the trace is the status and what to do.
    """
    retired = provider_failure(Retired())
    assert "410" in retired and "retired that model" in retired
    assert "error.log" in retired
    assert "deepseek-ai/deepseek-v4-pro-0813" not in retired, "the provider's body stays out"
    assert "end of life" not in retired

    rejected = provider_failure(Rejected())
    assert "403" in rejected and "refused access with this key" in rejected
    assert "Authorization failed" not in rejected

    # A failure with no status still says what to check, and still quotes nothing.
    plain = provider_failure(RuntimeError("SECRET request body"))
    assert "connection, provider or tool failed" in plain
    assert "SECRET" not in plain


def test_an_unroutable_identifier_that_reaches_the_engine_is_explained():
    class NotRouted(Exception):
        status_code = 400
        message = "litellm.BadRequestError: LLM Provider NOT provided. You passed model=moonshotai/kimi-k3"

    message = provider_failure(NotRouted())
    assert "could not route that model identifier" in message
    assert "moonshotai/kimi-k3" not in message
