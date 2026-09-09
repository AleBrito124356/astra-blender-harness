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
