import pytest
from fastapi.testclient import TestClient

from astra_blender.config import RunConfig
from astra_blender.server import create_app


def test_local_auth_origin_and_validation_redaction(tmp_path):
    with TestClient(create_app(output=tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/session", headers={"Host": "evil.example"}).status_code == 400
        assert client.get("/api/session", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post("/api/demo").status_code == 401
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        response = client.post(
            "/api/runs", headers=headers, json={"prompt": "x", "api_key": "secret-test-key"}
        )
        assert response.status_code == 422
        assert "secret-test-key" not in response.text
        assert client.get("/api/runs/missing", headers=headers).status_code == 404


@pytest.mark.parametrize(
    "url",
    ["http://example.com/v1", "https://user:pass@example.com", "file:///x", "https://example.com?key=secret"],
)
def test_reject_invalid_base(url):
    with pytest.raises(ValueError):
        RunConfig(prompt="create scene", api_base=url)


def test_allow_loopback():
    assert (
        RunConfig(prompt="create scene", api_base="http://localhost:11434/").api_base
        == "http://localhost:11434"
    )


def test_crashed_run_without_manifest_is_available_to_resume(tmp_path):
    import json

    run_id = "b" * 32
    directory = tmp_path / run_id
    directory.mkdir()
    (directory / "state.json").write_text(
        json.dumps({"prompt": "Build a lamp", "model": "test/model", "stage": "build", "messages": []})
    )
    (directory / "events.jsonl").write_text("")
    with TestClient(create_app(output=tmp_path), base_url="http://127.0.0.1") as client:
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        resumed = client.get("/api/runs/resumable", headers=headers).json()["runs"]
        assert resumed[0]["id"] == run_id and resumed[0]["status"] == "interrupted"
        assert client.get("/api/runs/history", headers=headers).json()["runs"][0]["model"] == "test/model"
        assert client.get("/api/runs/" + run_id, headers=headers).json()["archived"]
        assert client.get("/api/scene/live").status_code == 401
