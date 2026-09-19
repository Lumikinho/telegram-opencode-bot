from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_battery_fake_sysfs(tmp_path):
    base = tmp_path / "battery"
    base.mkdir()
    (base / "capacity").write_text("82\n")
    (base / "status").write_text("Discharging\n")
    (base / "temp").write_text("310\n")
    r = client.get("/api/battery", params={"battery_path": str(base)})
    assert r.status_code == 200
    body = r.json()
    assert body["capacity"] == "82"
    assert "82%" in body["formatted"]
    assert "descarregando" in body["formatted"]


def test_render_markdown():
    r = client.post("/api/render/markdown", json={"text": "**oi** `x`"})
    assert r.status_code == 200
    assert "<b>oi</b>" in r.json()["html"]
