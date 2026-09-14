import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from astra_blender.config import RunConfig
from astra_blender.engine import Run
from astra_blender.references import attach, message, normalize
from astra_blender.server import create_app
from astra_blender.spatial import diagnostics


def photo():
    out = io.BytesIO()
    Image.new("RGB", (1800, 900), "#bb8844").save(out, format="PNG")
    return out.getvalue()


def test_reference_normalized_and_vision_required(tmp_path):
    raw = normalize(photo())
    image = Image.open(io.BytesIO(raw))
    assert image.format == "JPEG" and image.size == (1536, 768)
    assert not image.getexif()
    source = tmp_path / "input.png"
    source.write_bytes(photo())
    run = Run(RunConfig(prompt="Model this photo", vision=False), tmp_path)
    with pytest.raises(ValueError, match="Vision"):
        attach(run, [source])
    run.config.vision = True
    attach(run, [source])
    assert message(run.directory)["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    with pytest.raises(ValueError):
        normalize(b"not a photo")


def test_upload_auth_attachment_resume_and_vision_gate(tmp_path, monkeypatch):
    async def execute(run, *args, **kwargs):
        assert list(run.directory.glob("reference-*.jpg"))
        run.status = "completed"
        run.save_state([{"role": "user", "content": "fixture"}], "build")

    monkeypatch.setattr("astra_blender.server.execute", execute)
    app = create_app(output=tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/references", content=photo()).status_code == 401
        headers = {"X-Astra-Token": client.get("/api/session").json()["token"]}
        assert client.post("/api/references", headers=headers, content=b"bad").status_code == 422
        uploaded = client.post("/api/references", headers=headers, content=photo()).json()
        body = {"prompt": "Model this image", "reference_ids": [uploaded["id"]], "vision": False}
        assert client.post("/api/runs", headers=headers, json=body).status_code == 422
        body["vision"] = True
        first = client.post("/api/runs", headers=headers, json=body).json()["id"]
        assert (tmp_path / first / "reference-1.jpg").is_file()
        assert client.delete("/api/references/" + uploaded["id"], headers=headers).status_code == 200
        # The run owns its normalized copy; source removal does not destroy it.
        resumed = client.post(
            "/api/runs",
            headers=headers,
            json={"prompt": "Continue modeling", "vision": True, "resume_from": first},
        ).json()["id"]
        assert (tmp_path / resumed / "reference-1.jpg").is_file()
        response = client.post(
            "/api/runs",
            headers=headers,
            json={"prompt": "Continue modeling", "vision": False, "resume_from": first},
        )
        assert response.status_code == 422
        assert client.post("/api/scene/frame", json={"frame": 10}).status_code == 401


def test_ground_and_explicit_detachment_evidence():
    ground = {
        "name": "Ground",
        "dimensions": [20, 20, 0],
        "bounds": [[-10, -10, 0], [10, 10, 0]],
        "materials": ["soil"],
    }
    body = {
        "name": "CarBody",
        "dimensions": [4, 2, 1],
        "bounds": [[-2, -1, 0], [2, 1, 1]],
        "materials": ["paint"],
    }
    glass = {
        "name": "CarWindow",
        "dimensions": [1, 0.1, 0.5],
        "bounds": [[5, 0, -1], [6, 0.1, -0.5]],
        "anchor": "CarBody",
        "max_gap": 0.15,
        "materials": ["glass"],
    }
    report = diagnostics({"objects": [ground, body, glass], "camera": None})
    assert any(i["code"] == "below_ground" and i["objects"] == ["CarWindow"] for i in report["issues"])
    gap = next(i for i in report["issues"] if i["code"] == "detached_part")
    assert gap["gap"] > 3 and gap["inferred_anchor"] is False


@pytest.mark.parametrize("prompt", ["Anima el coche", "Animate the car", "Create a rotating sculpture"])
def test_animation_auto_detection(prompt):
    assert RunConfig(prompt=prompt).wants_animation()
    assert not RunConfig(prompt=prompt, animation="off").wants_animation()
