from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_openapi_tem_turns():
    spec = client.get("/openapi.json").json()
    assert "/api/turns/new" in spec["paths"]
    assert "/api/battery" in spec["paths"]
    assert "/api/funnel/status" in spec["paths"]
    assert "/api/render/markdown" in spec["paths"]
