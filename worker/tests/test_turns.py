from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _new_turn():
    r = client.post("/api/turns/new", json={"chat_id": 1})
    assert r.status_code == 200
    return r.json()["turn"]


def test_new_fold_split_html():
    turn = _new_turn()
    r = client.post("/api/turns/fold", json={"turn": turn, "event": {"type": "x", "data": {}}})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] in ("none", "push", "push_force", "finish")
    assert isinstance(body["turn"], dict)

    r = client.post("/api/turns/split", json={"text": "linha\n" * 1000, "limit": 4000})
    assert r.status_code == 200
    chunks = r.json()["chunks"]
    assert len(chunks) == 2
    assert all(len(c) <= 4000 for c in chunks)

    r = client.post("/api/turns/telegram-html", json={"text": "**oi**"})
    assert r.status_code == 200
    assert "<b>oi</b>" in r.json()["html"]


def test_form_flow():
    turn = _new_turn()
    r = client.post("/api/turns/submit-form", json={"turn": turn, "request_id": "x"})
    assert r.status_code == 200
    assert r.json()["complete"] is True

    r = client.post("/api/turns/safe-filename", json={"name": "nota: final?.pdf", "default": "x.bin"})
    assert r.json()["filename"] == "nota_ final_.pdf"

    r = client.post("/api/turns/media-note", json={"kind": "oversize", "filename": "v.mp4", "size_mb": 25})
    assert "25 MiB" in r.json()["text"]

    r = client.get("/api/turns/media-limits")
    assert r.json()["max_bytes"] == 20 * 1024 * 1024
