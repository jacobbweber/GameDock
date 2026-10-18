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


# ====================================================================== step 3
def _seed(service, tmp_path):
    d = tmp_path / "data" / "one"
    (d / "conf").mkdir(parents=True, exist_ok=True)
    (d / "conf" / "settings.ini").write_text("AdminPassword=hunter2\nName=x\n")
    (d / "saves").mkdir(exist_ok=True)
    (d / "saves" / "w.dat").write_text("w1")


@pytest.fixture
def seeded(client, one, service, tmp_path):
    _seed(service, tmp_path)
    return one


def test_server_info_exposes_capabilities(client, one):
    s = client.get("/api/servers/one").json()
    assert s["update_method"] == "pull_recreate" and s["config_file_count"] == 3


def test_backup_endpoints_roundtrip(client, seeded, tmp_path):
    r = client.post("/api/servers/one/backups", json={})
    assert r.status_code == 201
    body = r.json()
    name = body["backup"]["name"]
    assert body["was_running"] is False and "consistent" in body["note"]
    assert client.post("/api/servers/one/backups").status_code == 201  # body is optional
    lst = client.get("/api/servers/one/backups").json()
    assert lst["retention"] == 10 and len(lst["backups"]) == 2
    assert {"name", "size", "created_at", "kind"} <= set(lst["backups"][0])

    (tmp_path / "data" / "one" / "saves" / "w.dat").write_text("changed")
    assert client.post(f"/api/servers/one/backups/{name}/restore").status_code == 400  # no confirm
    r = client.post(f"/api/servers/one/backups/{name}/restore?confirm=true")
    assert r.status_code == 200 and r.json()["safety_backup"]["kind"] == "pre-restore"
    assert (tmp_path / "data" / "one" / "saves" / "w.dat").read_text() == "w1"

    assert client.delete(f"/api/servers/one/backups/{name}").status_code == 400
    assert client.delete(f"/api/servers/one/backups/{name}?confirm=true").json() == {"deleted": name}
    assert client.delete(f"/api/servers/one/backups/{name}?confirm=true").status_code == 404


def test_backup_running_server_modes(client, seeded):
    client.post("/api/servers/one/start")
    safe = client.post("/api/servers/one/backups", json={}).json()
    assert safe["stopped_for_backup"] and safe["restarted"] and not safe["live"]
    live = client.post("/api/servers/one/backups", json={"live": True}).json()
    assert live["live"] and not live["stopped_for_backup"] and "NOT stopped" in live["note"]
    assert client.get("/api/servers/one").json()["status"] == "running"


def test_restore_running_is_409_and_bad_names_rejected(client, seeded):
    name = client.post("/api/servers/one/backups").json()["backup"]["name"]
    client.post("/api/servers/one/start")
    assert client.post(f"/api/servers/one/backups/{name}/restore?confirm=true").status_code == 409
    client.post("/api/servers/one/stop")
    assert client.post("/api/servers/one/backups/..%2F..%2Finstances.json/restore?confirm=true").status_code in (400, 404)
    assert client.post("/api/servers/one/backups/nope.tar.gz/restore?confirm=true").status_code in (400, 404)


def test_backup_permission_error_is_403(client, seeded, tmp_path):
    import os
    if os.geteuid() == 0:
        pytest.skip("root")
    f = tmp_path / "data" / "one" / "saves" / "locked.dat"
    f.write_text("x")
    f.chmod(0)
    try:
        r = client.post("/api/servers/one/backups")
        assert r.status_code == 403 and "owned by another user" in r.json()["error"]
    finally:
        f.chmod(0o644)


def test_delete_with_data_makes_backup_first(client, seeded, tmp_path):
    q = "?confirm=true&delete_data=true&confirm_delete_data=true"
    r = client.delete("/api/servers/one" + q)
    assert r.status_code == 200
    body = r.json()
    assert body["data_deleted"] and not body["data_kept"] and body["backup"].endswith(".tar.gz")
    assert not (tmp_path / "data" / "one").exists()
    assert (tmp_path / "data" / "backups" / "_deleted" / body["backup"]).is_file()
    assert client.get("/api/servers").json() == []


def test_delete_with_data_aborted_when_backup_fails(client, seeded, monkeypatch, tmp_path):
    from gamedock import backups as bk
    from gamedock.errors import AccessError
    monkeypatch.setattr(bk, "create_archive", lambda *a, **k: (_ for _ in ()).throw(AccessError("unreadable files")))
    r = client.delete("/api/servers/one?confirm=true&delete_data=true&confirm_delete_data=true")
    assert r.status_code == 403
    assert len(client.get("/api/servers").json()) == 1
    assert (tmp_path / "data" / "one" / "saves" / "w.dat").exists()


def test_wipe_data_endpoint(client, seeded, tmp_path):
    assert client.post("/api/servers/one/wipe-data").status_code == 400
    r = client.post("/api/servers/one/wipe-data?confirm=true")
    assert r.status_code == 200 and r.json()["safety_backup"]["kind"] == "pre-wipe"
    assert not (tmp_path / "data" / "one" / "saves" / "w.dat").exists()


def test_image_update_endpoints(client, seeded, backend):
    info = client.get("/api/servers/one/image").json()
    assert info["image"] == "busybox:latest" and info["image_id"]
    assert client.post("/api/servers/one/update-image").status_code == 400  # needs confirm
    same = client.post("/api/servers/one/update-image?confirm=true").json()
    assert same["changed"] is False
    backend.publish_new_image("busybox:latest")
    r = client.post("/api/servers/one/update-image?confirm=true").json()
    assert r["changed"] and r["before"] != r["after"] and r["backup"]["kind"] == "pre-update"


def test_config_endpoints(client, seeded, tmp_path):
    files = client.get("/api/servers/one/config").json()
    assert {f["path"] for f in files} == {"conf/settings.ini", "conf/hot.txt", "conf/data.json"}
    r = client.get("/api/servers/one/config/file", params={"path": "conf/settings.ini"}).json()
    assert "hunter2" not in r["content"] and r["masked"] and r["editable_now"]
    r["content"] = r["content"].replace("Name=x", "Name=y")
    saved = client.put("/api/servers/one/config/file", json={"path": "conf/settings.ini", "content": r["content"]})
    assert saved.status_code == 200 and saved.json()["backup"].endswith(".bak")
    text = (tmp_path / "data" / "one" / "conf" / "settings.ini").read_text()
    assert "Name=y" in text and "AdminPassword=hunter2" in text


def test_config_error_codes(client, seeded, tmp_path):
    put = lambda path, content: client.put("/api/servers/one/config/file", json={"path": path, "content": content})
    assert put("../../etc/passwd", "x").status_code == 404
    assert put("conf/data.json", "{bad").status_code == 400
    assert put("conf/hot.txt", "x" * 300_000).status_code == 413
    client.post("/api/servers/one/start")
    assert put("conf/settings.ini", "x").status_code == 409
    assert put("conf/hot.txt", "ok").status_code == 200
    assert client.get("/api/servers/one/config/file", params={"path": "nope"}).status_code == 404
    assert client.get("/api/servers/one/config/file").status_code == 422


def test_config_symlink_escape_is_400(client, seeded, tmp_path):
    import os, shutil
    out = tmp_path / "outside"
    out.mkdir()
    (out / "settings.ini").write_text("x")
    shutil.rmtree(tmp_path / "data" / "one" / "conf")
    os.symlink(out, tmp_path / "data" / "one" / "conf")
    r = client.get("/api/servers/one/config/file", params={"path": "conf/settings.ini"})
    assert r.status_code == 400 and "escapes" in r.json()["error"]


def test_concurrent_operation_is_409(client, seeded, service):
    with service._operation("one"):
        assert client.post("/api/servers/one/backups").status_code == 409
        assert client.post("/api/servers/one/start").status_code == 409


def test_cross_origin_destructive_requests_refused(client, seeded):
    evil = {"origin": "http://evil.example"}
    assert client.post("/api/servers/one/wipe-data?confirm=true", headers=evil).status_code == 403
    assert client.post("/api/servers/one/update-image?confirm=true", headers=evil).status_code == 403
    assert client.put("/api/servers/one/config/file", json={"path": "conf/hot.txt", "content": "x"},
                      headers=evil).status_code == 403
