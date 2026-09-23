import time

from fastapi.testclient import TestClient

from astra_blender import server
from astra_blender.server import create_app

STATIC = server.Path(server.__file__).parent / "static"


def client_for(tmp_path):
    return TestClient(create_app(output=tmp_path), base_url="http://127.0.0.1")


def test_a_pull_while_the_server_runs_is_reported(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get("/api/session").json()["assets_newer_than_server"] is False
        # A git pull, or an edit, touches the files the browser loads.
        touched = STATIC / "app.js"
        original = touched.stat().st_mtime
        try:
            future = time.time() + 60
            import os

            os.utime(touched, (future, future))
            assert client.get("/api/session").json()["assets_newer_than_server"] is True
        finally:
            os.utime(touched, (original, original))
        assert client.get("/api/session").json()["assets_newer_than_server"] is False


def test_an_unknown_setting_names_itself_and_the_cause(tmp_path):
    # A page newer than the backend sends a field the old RunConfig forbids.
    with client_for(tmp_path) as client:
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        response = client.post(
            "/api/runs",
            headers=headers,
            json={
                "prompt": "Create a lamp",
                "setting_from_a_newer_page": 3000,
                "api_key": "sk-not-a-real-key",
            },
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "setting_from_a_newer_page" in detail
        assert "Restart astra-blender serve" in detail
        # Names, never values: the key is submitted alongside and must not appear.
        assert "sk-not-a-real-key" not in response.text


def test_a_bad_value_names_the_field_without_its_value(tmp_path):
    with client_for(tmp_path) as client:
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        response = client.post(
            "/api/runs", headers=headers, json={"prompt": "x", "api_key": "sk-not-a-real-key"}
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "prompt" in detail
        assert "sk-not-a-real-key" not in response.text
