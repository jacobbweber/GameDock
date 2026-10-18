import pytest
from fastapi.testclient import TestClient

from gamedock.web.app import create_app


@pytest.fixture
def client(service):
    return TestClient(create_app(service))


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "GameDock" in r.text


def test_list_and_lifecycle(client):
    assert client.get("/api/servers").json()[0]["status"] == "not_created"
    assert client.post("/api/servers/sample/start").json()["status"] == "running"
    assert client.get("/api/servers/sample").json()["status"] == "running"
    assert client.post("/api/servers/sample/restart").json()["status"] == "running"
    assert client.post("/api/servers/sample/stop").json()["status"] == "stopped"


def test_unknown_is_404(client):
    assert client.get("/api/servers/nope").status_code == 404
    assert client.post("/api/servers/nope/start").status_code == 404


def test_backend_failure_is_502(client, backend):
    backend.fail_next = "start"
    r = client.post("/api/servers/sample/start")
    assert r.status_code == 502 and "simulated" in r.json()["error"]


def test_cross_origin_post_refused(client):
    r = client.post("/api/servers/sample/start", headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/servers/sample").json()["status"] == "not_created"


def test_same_origin_post_allowed(client):
    r = client.post("/api/servers/sample/start", headers={"origin": "http://testserver"})
    assert r.status_code == 200


def test_default_settings_bind_localhost():
    from gamedock.config import Settings
    assert Settings().host == "127.0.0.1"
