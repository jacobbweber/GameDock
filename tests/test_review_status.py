"""0.6.0: get_catalog_review_status, preview_deploy and the "add -> approve -> deploy a test copy -> fix -> re-review" flow.
Deploying an entry nobody approved stays blocked."""
import json

import pytest
from fastapi.testclient import TestClient

from gamedock import authoring as au
from gamedock.errors import ConflictError, NotFoundError
from gamedock.web.app import create_app
from tests.conftest import ADMIN
from tests.mcp_helpers import call, make_app, run
from tests.test_authoring import GOOD, good

SECRET = "hunter2-very-secret"


def fingerprint(service, gid):
    return au.sha256_bytes((service.catalog_dir / f"{gid}.yaml").read_bytes())


@pytest.fixture
def added(service):
    r = service.add_catalog_entry(GOOD)
    return r["id"]


# ------------------------------------------------------------------ review status
def test_status_of_a_new_ai_entry_is_waiting(service, added):
    r = service.catalog_review_status(added)
    assert r["state"] == "waiting_for_approval" and r["reviewed"] is False and r["can_deploy"] is False
    assert r["origin"] == "mcp" and r["fingerprint"] == fingerprint(service, added) and r["fingerprint_short"] == r["fingerprint"][:12]
    assert r["reviewed_fingerprint"] is None and r["reviewed_at"] is None and r["added_at"]
    assert "approve" in r["next_step"].lower() and "yaml" not in r


def test_status_after_approval_shows_the_approved_fingerprint(service, added):
    service.approve_catalog_entry(added, fingerprint(service, added))
    r = service.catalog_review_status(added)
    assert r["state"] == "approved" and r["reviewed"] and r["can_deploy"] and r["changed_since_review"] is False
    assert r["reviewed_fingerprint"] == r["fingerprint"] and r["reviewed_at"]
    assert "preview_deploy" in r["next_step"] and "verify_game_setup" in r["next_step"]


def test_status_notices_a_hand_edit_after_approval(service, added):
    service.approve_catalog_entry(added, fingerprint(service, added))
    path = service.catalog_dir / f"{added}.yaml"
    path.write_text(path.read_text() + "# edited by hand\n")
    r = service.catalog_review_status(added)
    assert r["state"] == "changed_since_approval" and r["changed_since_review"] is True and r["can_deploy"] is False
    assert r["reviewed_fingerprint"] != r["fingerprint"] and r["last_approved_at"]


def test_status_after_update_needs_a_new_approval(service, added):
    service.approve_catalog_entry(added, fingerprint(service, added))
    service.update_catalog_entry(good(description="A better description."))
    r = service.catalog_review_status(added)
    assert r["state"] == "waiting_for_approval" and r["updated_at"] and r["last_approved_at"]
    assert "earlier version was approved" in r["summary"] and "deploy it again" in r["next_step"]


def test_status_for_repository_entries_and_unknown_ids(service):
    r = service.catalog_review_status("sample")
    assert r["state"] == "repository" and r["origin"] == "repo" and r["can_deploy"] and r["reviewed"]
    with pytest.raises(NotFoundError) as e:
        service.catalog_review_status("nope")
    assert "sample" in str(e.value)


def test_status_can_include_the_yaml_but_only_on_request(service, added):
    assert "ports:" in service.catalog_review_status(added, include_yaml=True)["yaml"]
    assert "yaml" not in service.catalog_review_status(added)


def test_status_lists_servers_on_an_older_snapshot(service, added):
    service.approve_catalog_entry(added, fingerprint(service, added))
    service.deploy(added, "Test copy", env={"SERVER_NAME": "t"})
    assert service.catalog_review_status(added)["servers"] == [
        {"server_id": "test-copy", "name": "Test copy", "uses_current_entry": True, "differences": []}]
    service.update_catalog_entry(good(image="example/test-game-server:1.3"))
    service.approve_catalog_entry(added, fingerprint(service, added))
    r = service.catalog_review_status(added)
    assert r["servers_on_older_snapshot"] == ["test-copy"] and "image" in r["servers"][0]["differences"]
    assert "delete and deploy again" in r["servers_note"]


def test_status_never_approves_or_changes_anything(service, added):
    before = (service.data_dir / au.REVIEW_FILENAME).read_text()
    for _ in range(3):
        service.catalog_review_status(added, include_yaml=True)
    assert (service.data_dir / au.REVIEW_FILENAME).read_text() == before


# ------------------------------------------------------------------ MCP and web
def test_mcp_status_tool_in_every_mode_and_deploy_stays_blocked(service, added):
    for mode in ("read_only", "standard", "full"):
        err, r = run(make_app(service, mode), lambda c: call(c, "get_catalog_review_status", game_id=added))
        assert not err and r["state"] == "waiting_for_approval"
    err, msg = run(make_app(service, "standard"), lambda c: call(c, "deploy_server", game_id=added, name="x", env={"SERVER_NAME": "a"}))
    assert err and "approved" in msg
    assert service.store.all() == []
    err, msg = run(make_app(service, "standard"), lambda c: call(c, "get_catalog_review_status", game_id="nope"))
    assert err and "Unknown catalog entry" in msg


def test_mcp_status_is_audited_with_state_but_never_the_yaml(service, added):
    app = make_app(service, "standard")
    run(app, lambda c: call(c, "get_catalog_review_status", game_id=added, include_yaml=True))
    line = json.loads((service.data_dir / "mcp-audit.jsonl").read_text().splitlines()[-1])
    assert line["tool"] == "get_catalog_review_status" and "state=waiting_for_approval" in line["detail"]
    assert "ports:" not in json.dumps(line)


def test_web_status_endpoint(service, added):
    with TestClient(create_app(service)) as c:
        r = c.get(f"/api/catalog/{added}/review-status")
        assert r.status_code == 200 and r.json()["state"] == "waiting_for_approval"
        assert c.get("/api/catalog/zzz/review-status").status_code == 404


def test_there_is_still_no_way_to_approve_over_mcp(service, added):
    err, r = run(make_app(service, "full"), lambda c: call(c, "get_catalog_review_status", game_id=added))
    assert not err and "approve" in r["next_step"].lower() and r["can_deploy"] is False
    with pytest.raises(ConflictError):
        service.deploy(added, "x", env={"SERVER_NAME": "a"})


# ------------------------------------------------------------------ preview_deploy
def test_preview_of_an_unapproved_entry_explains_and_creates_nothing(service, backend, added):
    r = service.preview_deploy(added, "Test copy", env={"SERVER_NAME": "t", "SERVER_PASSWORD": SECRET})
    assert r["ok"] is False and r["can_deploy"] is False and r["reviewed"] is False
    assert any("not approved" in p or "has not approved" in p for p in r["problems"])
    assert any("get_catalog_review_status" in p for p in r["problems"])
    assert service.store.all() == [] and not (service.data_dir / "test-copy").exists()
    assert not [c for c in backend.calls if c[0] in ("create", "start", "pull")]


def test_preview_of_an_approved_entry_shows_the_plan_with_secrets_masked(service, backend, added):
    service.approve_catalog_entry(added, fingerprint(service, added))
    r = service.preview_deploy(added, "Test copy", env={"SERVER_NAME": "t", "SERVER_PASSWORD": SECRET})
    assert r["ok"] and r["can_deploy"] and r["problems"] == [] and r["server_id"] == "test-copy"
    assert r["ports"][0] == {"protocol": "udp", "host": 27100, "container": 27015}
    assert r["volumes"][0]["host_path"].endswith("test-copy/saves") and r["volumes"][0]["exists"] is False
    cmd = r["container_command"]
    assert cmd.startswith("docker create --name gamedock-test-copy") and "-p 27100:27015/udp" in cmd and "example/test-game-server:1.2" in cmd
    assert SECRET not in json.dumps(r) and "-e SERVER_PASSWORD" in cmd
    env = {e["name"]: e for e in r["env"]}
    assert env["SERVER_PASSWORD"]["value"] == "(set, hidden)" and env["SERVER_NAME"]["value"] == "t" and env["MODE"]["value"] == "1"
    assert r["image_state"].startswith("not stored here") and r["read_only"] is True
    assert service.store.all() == [] and not (service.data_dir / "test-copy").exists()
    assert not [c for c in backend.calls if c[0] in ("create", "start", "pull")]


def test_preview_collects_every_problem(service, backend, deployed):
    # 'sample' is deployed as 'one' on 27015/udp. Same name, missing required field, port clash: all reported at once.
    r = service.preview_deploy("sample", "One", env={})
    text = " ".join(r["problems"])
    assert r["ok"] is False and "already exists" in text and "ADMIN_PASSWORD" in text and "already used by your server" in text
    assert any(f["suggested_host_port"] for f in r["port_check"]["findings"])
    assert "Try host port" in text


def test_preview_unknown_game_and_bad_ports(service):
    with pytest.raises(NotFoundError):
        service.preview_deploy("nope", "x")
    from gamedock.errors import ValidationError
    from gamedock.models import PortMapping
    r = service.preview_deploy("sample", "x", ports=[PortMapping("udp", 1, 9999)], env=ADMIN)
    assert not r["ok"] and "no container port 9999/udp" in " ".join(r["problems"])


def test_preview_mcp_tool_and_web_endpoint(service, deployed):
    err, r = run(make_app(service, "read_only"), lambda c: call(c, "preview_deploy", game_id="sample", name="Two", env=ADMIN,
                                                                ports=[{"protocol": "udp", "container": 27015, "host": 27020}]))
    assert not err and r["ok"] and r["ports"][0]["host"] == 27020
    assert service.store.get("two") is None
    with TestClient(create_app(service)) as c:
        w = c.post("/api/catalog/sample/deploy-preview", json={"name": "Two", "env": ADMIN})
        assert w.status_code == 200 and w.json()["ok"] is False        # 27015/udp is used by 'One'
        assert c.post("/api/catalog/nope/deploy-preview", json={"name": "x"}).status_code == 404
