import json

import pytest
from fastapi.testclient import TestClient

from gamedock.web.app import create_app

DEPLOY = {"game_id": "sample", "name": "One", "env": {"ADMIN_PASSWORD": "adminpw"}}


@pytest.fixture
def client(service):
    return TestClient(create_app(service))


@pytest.fixture
def one(client):
    r = client.post("/api/servers", json=DEPLOY)
    assert r.status_code == 201
    return r.json()


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "GameDock" in r.text


def test_catalog_endpoints(client):
    cat = client.get("/api/catalog").json()
    assert [e["id"] for e in cat] == ["sample"]
    assert cat[0]["ports"][0] == {"protocol": "udp", "host": 27015, "container": 27015}
    assert any(f["name"] == "ADMIN_PASSWORD" and f["secret"] for f in cat[0]["fields"])
    assert client.get("/api/catalog/sample").status_code == 200
    assert client.get("/api/catalog/nope").status_code == 404


def test_no_servers_until_deployed(client):
    assert client.get("/api/servers").json() == []


def test_deploy_returns_201_and_hides_secrets(client, one):
    assert one["id"] == "one" and one["status"] == "stopped"
    assert "adminpw" not in json.dumps(one)
    assert "adminpw" not in client.get("/api/servers").text
    assert "adminpw" not in client.get("/api/servers/one").text


def test_deploy_with_start(client):
    r = client.post("/api/servers", json={**DEPLOY, "start": True})
    assert r.status_code == 201 and r.json()["status"] == "running"


def test_lifecycle(client, one):
    assert client.post("/api/servers/one/start").json()["status"] == "running"
    assert client.post("/api/servers/one/restart").json()["status"] == "running"
    assert client.post("/api/servers/one/stop").json()["status"] == "stopped"


def test_port_conflict_is_409(client, one):
    r = client.post("/api/servers", json={**DEPLOY, "name": "Two"})
    assert r.status_code == 409 and "already used" in r.json()["error"]
    ok = client.post("/api/servers", json={**DEPLOY, "name": "Two",
                     "ports": [{"protocol": "udp", "container": 27015, "host": 27016}]})
    assert ok.status_code == 201


def test_validation_errors(client):
    r = client.post("/api/servers", json={"game_id": "sample", "name": "A"})
    assert r.status_code == 400 and "required" in r.json()["error"]
    assert client.post("/api/servers", json={"name": "A"}).status_code == 422
    assert client.post("/api/servers", json={**DEPLOY, "game_id": "nope"}).status_code == 404
    bad = client.post("/api/servers", json={**DEPLOY, "ports": [{"protocol": "icmp", "container": 1, "host": 1}]})
    assert bad.status_code == 422


def test_update(client, one):
    r = client.patch("/api/servers/one", json={"env": {"MAX": "12"}, "name": "Renamed"})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    client.post("/api/servers/one/start")
    assert client.patch("/api/servers/one", json={"env": {"MAX": "1"}}).status_code == 409


def test_delete_needs_confirm(client, one):
    r = client.delete("/api/servers/one")
    assert r.status_code == 400 and "confirmation" in r.json()["error"]
    assert len(client.get("/api/servers").json()) == 1
    assert client.delete("/api/servers/one?confirm=true").json()["data_kept"] is True
    assert client.get("/api/servers").json() == []


def test_delete_data_flow(client, one):
    r = client.delete("/api/servers/one?confirm=true&delete_data=true")
    assert r.status_code == 400 and "second" in r.json()["error"]
    assert len(client.get("/api/servers").json()) == 1


def test_delete_running_is_409(client, one):
    client.post("/api/servers/one/start")
    assert client.delete("/api/servers/one?confirm=true").status_code == 409


def test_unknown_is_404(client):
    assert client.get("/api/servers/nope").status_code == 404
    assert client.post("/api/servers/nope/start").status_code == 404
    assert client.delete("/api/servers/nope?confirm=true").status_code == 404


def test_backend_failure_is_502(client, one, backend):
    backend.fail_next = "start"
    r = client.post("/api/servers/one/start")
    assert r.status_code == 502 and "simulated" in r.json()["error"]


def test_cross_origin_writes_refused(client, one):
    for call in (lambda h: client.post("/api/servers/one/start", headers=h),
                 lambda h: client.delete("/api/servers/one?confirm=true", headers=h),
                 lambda h: client.post("/api/servers", json=DEPLOY, headers=h)):
        assert call({"origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/servers/one").json()["status"] == "stopped"
    assert len(client.get("/api/servers").json()) == 1


def test_same_origin_post_allowed(client, one):
    r = client.post("/api/servers/one/start", headers={"origin": "http://testserver"})
    assert r.status_code == 200


def test_default_settings_bind_localhost():
    from gamedock.config import Settings
    assert Settings().host == "127.0.0.1"
