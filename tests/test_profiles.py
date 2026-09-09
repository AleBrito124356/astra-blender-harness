import json

import pytest
from fastapi.testclient import TestClient

from astra_blender import catalog, profiles
from astra_blender.server import create_app

SECRET = "sk-do-not-write-me"


class FakeKeyring:
    """Stands in for the OS keyring so tests never touch the real credential store."""

    def __init__(self):
        self.store = {}

    def set_password(self, service, name, secret):
        self.store[(service, name)] = secret

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        self.store.pop((service, name), None)

    def get_keyring(self):
        return self


def a_profile(name="DeepSeek Pro"):
    return profiles.Profile(name=name, provider="deepseek", model="deepseek/deepseek-v4-pro", vision=False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(profiles, "config_dir", lambda: tmp_path / "astra")
    monkeypatch.setattr(profiles, "_keyring", lambda: fake)
    return fake


def test_secret_goes_to_the_keyring_and_never_to_disk(store, tmp_path):
    profiles.save(a_profile(), SECRET)
    body = (tmp_path / "astra" / "profiles.json").read_text(encoding="utf-8")
    assert SECRET not in body
    assert "deepseek/deepseek-v4-pro" in body
    assert store.store[(profiles.SERVICE, "DeepSeek Pro")] == SECRET
    listed = profiles.listing()
    assert listed[0]["has_secret"] is True
    assert SECRET not in json.dumps(listed)


def test_saving_without_a_key_leaves_any_existing_one_alone(store):
    profiles.save(a_profile(), SECRET)
    result = profiles.save(a_profile(), None)
    assert result["remembered"] is False
    assert store.store[(profiles.SERVICE, "DeepSeek Pro")] == SECRET


def test_delete_removes_the_profile_and_its_secret(store, tmp_path):
    profiles.save(a_profile(), SECRET)
    profiles.delete("DeepSeek Pro")
    assert profiles.listing() == []
    assert store.store == {}


def test_without_a_keyring_the_profile_saves_but_the_key_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "config_dir", lambda: tmp_path / "astra")
    monkeypatch.setattr(profiles, "_keyring", lambda: None)
    with pytest.raises(ValueError, match="keyring"):
        profiles.save(a_profile(), SECRET)
    # The non-secret half still persisted, and the key reached no file anywhere.
    assert profiles.listing()[0]["has_secret"] is False
    assert SECRET not in (tmp_path / "astra" / "profiles.json").read_text(encoding="utf-8")
    assert profiles.secret_backend() is None


def test_rejects_an_unusable_profile_name(store):
    with pytest.raises(ValueError):
        profiles.save(profiles.Profile(name="../escape", model="deepseek/deepseek-v4-pro"), None)


def test_a_corrupt_file_does_not_break_listing(store, tmp_path):
    profiles.save(a_profile(), None)
    (tmp_path / "astra" / "profiles.json").write_text("{not json", encoding="utf-8")
    assert profiles.listing() == []


def test_every_catalog_id_carries_its_provider_prefix():
    # A bare display name is what made a real run fail with LiteLLM's
    # "LLM Provider NOT provided", so the picker must only offer routable IDs.
    groups = catalog.models()
    identifiers = [model["id"] for group in groups for model in group["models"]]
    assert identifiers, "the installed LiteLLM should expose chat models"
    assert all("/" in identifier for identifier in identifiers)
    assert "Deepseek V4 Pro" not in identifiers
    assert "deepseek/deepseek-v4-pro" in identifiers


def test_catalog_reports_capabilities():
    model = catalog.describe("deepseek/deepseek-v4-pro")
    assert model["tools"] is True
    assert model["vision"] is False
    assert catalog.describe("nope/not-a-model") is None


def client_for(tmp_path):
    return TestClient(create_app(output=tmp_path), base_url="http://127.0.0.1")


def test_profile_endpoints_require_auth_and_hide_secrets(store, tmp_path):
    with client_for(tmp_path) as client:
        assert client.get("/api/profiles").status_code == 401
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        saved = client.put(
            "/api/profiles",
            headers=headers,
            json={
                "name": "DeepSeek Pro",
                "provider": "deepseek",
                "model": "deepseek/deepseek-v4-pro",
                "api_key": SECRET,
            },
        )
        assert saved.status_code == 200 and saved.json()["remembered"] is True
        listing = client.get("/api/profiles", headers=headers)
        assert SECRET not in listing.text
        assert listing.json()["profiles"][0]["has_secret"] is True
        assert client.delete("/api/profiles/DeepSeek Pro", headers=headers).status_code == 200
        assert client.get("/api/profiles", headers=headers).json()["profiles"] == []


def test_run_resolves_the_remembered_key_server_side(store, tmp_path, monkeypatch):
    profiles.save(a_profile(), SECRET)
    seen = {}

    async def fake_execute(run, config, **kwargs):
        seen["key"] = run.config.api_key.get_secret_value()
        run.status = "completed"

    monkeypatch.setattr("astra_blender.server.execute", fake_execute)
    with client_for(tmp_path) as client:
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        started = client.post(
            "/api/runs",
            headers=headers,
            # The browser sends no key at all, only the profile name.
            json={
                "prompt": "build a lamp",
                "model": "deepseek/deepseek-v4-pro",
                "profile": "DeepSeek Pro",
            },
        )
        assert started.status_code == 200
        run_id = started.json()["id"]
        for _ in range(50):
            if client.get(f"/api/runs/{run_id}", headers=headers).json()["status"] == "completed":
                break
    assert seen["key"] == SECRET


def test_routable_identifiers_for_any_provider():
    # A gateway model ID carries its own slashes; the provider prefix goes in
    # front of the whole thing, which is what LiteLLM routes on.
    assert catalog.routable("nvidia_nim", "deepseek-ai/deepseek-v4-pro-0813") == (
        "nvidia_nim/deepseek-ai/deepseek-v4-pro-0813"
    )
    # An unknown provider is reached through the OpenAI-compatible route.
    assert catalog.routable("custom", "my-local-model") == "openai/my-local-model"


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def fake_client(response):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            Client.seen = {"url": url, "headers": headers or {}}
            return response

    return Client


async def test_discovery_prefixes_what_the_provider_lists(monkeypatch):
    import httpx

    payload = {"data": [{"id": "deepseek-ai/deepseek-v4-pro-0813"}, {"id": "meta/llama-3.3-70b"}]}
    client = fake_client(FakeResponse(200, payload))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    found = await catalog.discover("nvidia_nim", None, "nvapi-test")
    assert [m["id"] for m in found] == [
        "nvidia_nim/deepseek-ai/deepseek-v4-pro-0813",
        "nvidia_nim/meta/llama-3.3-70b",
    ]
    assert found[0]["raw"] == "deepseek-ai/deepseek-v4-pro-0813"
    # The default base is used when none is supplied, with a bearer token.
    assert client.seen["url"] == "https://integrate.api.nvidia.com/v1/models"
    assert client.seen["headers"]["Authorization"] == "Bearer nvapi-test"


async def test_discovery_reports_a_rejected_key_without_echoing_it(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", fake_client(FakeResponse(401, {})))
    with pytest.raises(ValueError, match="rejected the key"):
        await catalog.discover("nvidia_nim", None, "nvapi-secret-value")


def test_discovery_endpoint_rejects_an_unsafe_base(store, tmp_path):
    with client_for(tmp_path) as client:
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        response = client.post(
            "/api/models/discover",
            headers=headers,
            json={"provider": "custom", "api_base": "http://169.254.169.254/latest"},
        )
        assert response.status_code == 400
        assert "HTTPS" in response.json()["detail"]


def a_stopped_run(root, run_id="0" * 32, stage="build"):
    directory = root / run_id
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stage": stage,
                "steps": 7,
                "prompt": "Una casa sencilla",
                "messages": [{"role": "user", "content": "earlier work"}],
            }
        ),
        encoding="utf-8",
    )
    (directory / "manifest.json").write_text(json.dumps({"status": "budget_exhausted"}), encoding="utf-8")
    return run_id


def test_resumable_lists_only_unfinished_runs(store, tmp_path):
    a_stopped_run(tmp_path, "a" * 32)
    finished = tmp_path / ("b" * 32)
    finished.mkdir()
    (finished / "state.json").write_text(json.dumps({"stage": "finalize"}), encoding="utf-8")
    (finished / "manifest.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    with client_for(tmp_path) as client:
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        listed = client.get("/api/runs/resumable", headers=headers).json()["runs"]
    assert [entry["id"] for entry in listed] == ["a" * 32]
    assert listed[0]["stage"] == "build"


def test_a_run_resumes_from_the_saved_state(store, tmp_path, monkeypatch):
    run_id = a_stopped_run(tmp_path)
    seen = {}

    async def fake_execute(run, config, provider=None, connector=None, state=None):
        seen["state"] = state
        run.status = "completed"

    monkeypatch.setattr("astra_blender.server.execute", fake_execute)
    with client_for(tmp_path) as client:
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        started = client.post(
            "/api/runs",
            headers=headers,
            json={"prompt": "continue the house", "model": "openai/gpt-6-astra", "resume_from": run_id},
        )
        assert started.status_code == 200
        for _ in range(50):
            if (
                client.get(f"/api/runs/{started.json()['id']}", headers=headers).json()["status"]
                == "completed"
            ):
                break
        # An id with no saved state is refused rather than silently starting over.
        missing = client.post(
            "/api/runs",
            headers=headers,
            json={"prompt": "continue the house", "resume_from": "f" * 32},
        )
        assert missing.status_code == 404
    assert seen["state"]["stage"] == "build"
    assert seen["state"]["messages"][0]["content"] == "earlier work"
