"""Local HTTP boundaries, command ownership, and application lifecycle."""
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from orchid_demo.api import create_app
from orchid_demo.engine import Engine


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def operator_headers(client):
    token = client.get("/api/session").json()["token"]
    return {"x-orchid-token": token, "x-orchid-operator": str(uuid4())}


def wait_command(client, identity):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        result = client.get("/api/state").json()
        if not result["pending"] and result["last_receipt"] and result["last_receipt"]["id"] == identity:
            return result
        threading.Event().wait(0.02)
    pytest.fail("Worker did not process command")


def test_startup_has_no_hardware_side_effects_and_serves_ui(client):
    state = client.get("/api/state").json()
    assert state["phase"] == "disconnected"
    assert state["mode"] == "simulation"
    assert list(state["keys"]) == ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    assert client.get("/api/ports").json()["ports"][0]["path"] == "simulator"
    response = client.get("/")
    assert response.status_code == 200
    assert "Teach the instrument" in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/static/app.js").status_code == 200


def test_csrf_host_origin_and_size_checks(client):
    assert client.post("/api/heartbeat", json={}).status_code == 403
    headers = operator_headers(client)
    assert client.post("/api/heartbeat", json={}, headers={**headers, "origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/session", headers={"host": "evil.example"}).status_code == 400
    assert client.post("/api/heartbeat", json={}, headers={**headers, "origin": "http://127.0.0.1"}).status_code == 200
    assert client.post("/api/commands", content="x" * 16385, headers=headers).status_code == 413
    assert client.post("/api/commands", content=iter([b"{}"]), headers=headers).status_code == 411
    assert client.post("/api/heartbeat", json={}, headers={**headers, "x-orchid-operator": "invalid"}).status_code == 400


def test_single_operator_idempotency_stale_and_export(client):
    headers = operator_headers(client)
    assert client.post("/api/heartbeat", json={}, headers=headers).status_code == 200
    assert client.post("/api/heartbeat", json={}, headers={**headers, "x-orchid-operator": str(uuid4())}).status_code == 409
    cmd = {"id":str(uuid4()), "action":"connect", "revision":0, "args":{"prepared":True, "fixture":"API practice"}}
    assert client.post("/api/commands", json=cmd, headers=headers).status_code == 202
    state = wait_command(client, cmd["id"])
    assert state["phase"] == "connected"
    assert state["torque"] and all(v == 0 for v in state["torque"].values())
    assert client.post("/api/commands", json=cmd, headers=headers).json()["status"] == "complete"
    assert client.post("/api/commands", json={**cmd, "id":str(uuid4())}, headers=headers).status_code == 409
    assert client.post("/api/commands", json={**cmd, "revision":"1"}, headers=headers).status_code == 422
    assert client.post("/api/commands", json={**cmd, "action":"calibrate"}, headers=headers).status_code == 409
    exported = client.get("/api/export")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.json()["mode"] == "simulation"
    assert exported.json()["fixture"]["label"] == "API practice"


def test_app_close_stops_worker(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1"):
        assert app.state.engine.thread.is_alive()
    assert not app.state.engine.thread.is_alive()


def test_only_one_server_per_data_directory(tmp_path):
    first = create_app(tmp_path)
    second = create_app(tmp_path)
    try:
        with TestClient(first, base_url="http://127.0.0.1"):
            with pytest.raises(RuntimeError, match="already owns"):
                with TestClient(second, base_url="http://127.0.0.1"):
                    pytest.fail("Second server started")
    finally:
        second.state.engine.close()


def test_hardware_mode_never_connects_on_startup(tmp_path):
    e = Engine(tmp_path, "hardware", hardware_factory=lambda *_: pytest.fail("Connected on startup"))
    app = create_app(tmp_path, "hardware", engine=e)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/api/state").json()["connected"] is False
