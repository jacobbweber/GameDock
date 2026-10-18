"""0.6.0: container facts from Docker (parse + backend), the diagnostics bundle (redaction), the MCP audit tool and page
section, the catalog lint, and the guarantees around the new tools (read-only, nothing to approve)."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamedock.backends import DockerCliBackend
from gamedock.backends.container_inspect import parse_container_details
from gamedock.errors import BackendError, ValidationError
from gamedock.lint import lint_catalog
from gamedock.mcp_api import tools as mcp_tools
from gamedock.web.app import create_app
from tests.conftest import ADMIN
from tests.mcp_helpers import call, make_app, run
from tests.test_authoring import Recorder

ROOT = Path(__file__).resolve().parent.parent

INSPECT = json.dumps([{
    "Name": "/gamedock-one", "RestartCount": 3,
    "State": {"Status": "exited", "ExitCode": 137, "OOMKilled": True, "StartedAt": "2026-01-01T10:00:00Z",
              "FinishedAt": "2026-01-01T10:05:00Z", "Error": "", "Health": {"Status": "unhealthy"}},
    "Config": {"Env": ["ADMIN_PASSWORD=super-secret-pw", "PATH=/bin"], "Image": "x/y:1",
               "Labels": {"gamedock.managed": "true"}},
    "NetworkSettings": {"Ports": {"27015/udp": [{"HostIp": "0.0.0.0", "HostPort": "27015"}], "8211/tcp": None}},
    "Mounts": [{"Type": "bind", "Source": "/srv/one/saves", "Destination": "/saves", "RW": True}]}])


# ------------------------------------------------------------------ container facts
def test_parse_container_details():
    d = parse_container_details(INSPECT)
    assert d["status"] == "exited" and d["exit_code"] == 137 and d["oom_killed"] is True and d["restart_count"] == 3
    assert d["health"] == "unhealthy" and d["started_at"].startswith("2026-01-01T10:00")
    assert d["ports"] == [{"container": 27015, "protocol": "udp", "host_ip": "0.0.0.0", "host_port": 27015}]
    assert d["unpublished"] == ["8211/tcp"]
    assert d["mounts"] == [{"source": "/srv/one/saves", "target": "/saves", "writable": True, "type": "bind"}]
    assert "super-secret-pw" not in json.dumps(d)       # the container's environment is never read


@pytest.mark.parametrize("bad", ["", "not json", "[]", "42", '"x"'])
def test_parse_container_details_tolerates_junk(bad):
    assert parse_container_details(bad) is None


def test_parse_container_details_is_bounded():
    big = json.loads(INSPECT)
    big[0]["Mounts"] = [{"Source": f"/s{i}", "Destination": f"/d{i}"} for i in range(500)]
    big[0]["NetworkSettings"]["Ports"] = {f"{p}/tcp": [{"HostIp": "", "HostPort": str(p)}] for p in range(1000, 1500)}
    d = parse_container_details(json.dumps(big))
    assert len(d["mounts"]) <= 60 and len(d["ports"]) <= 60


def test_docker_backend_container_details_uses_one_read_only_inspect():
    rec = Recorder([(0, INSPECT, "")])
    d = DockerCliBackend(runner=rec).container_details("gamedock-one")
    assert d["exit_code"] == 137 and len(rec.calls) == 1
    assert rec.calls[0][1:3] == ["container", "inspect"] and rec.calls[0][-1] == "gamedock-one"


def test_docker_backend_container_details_missing_container_is_none():
    rec = Recorder([(1, "", "Error: No such container: gamedock-one")])
    assert DockerCliBackend(runner=rec).container_details("gamedock-one") is None


def test_docker_backend_container_details_refuses_foreign_containers_and_reports_docker_down():
    foreign = json.loads(INSPECT)
    foreign[0]["Config"]["Labels"] = {}
    with pytest.raises(BackendError) as e:
        DockerCliBackend(runner=Recorder([(0, json.dumps(foreign), "")])).container_details("gamedock-one")
    assert "was not created by GameDock" in str(e.value)
    with pytest.raises(BackendError):
        DockerCliBackend(runner=Recorder([(1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")])
                         ).container_details("gamedock-one")


def test_fake_backend_container_details(backend, service):
    assert backend.container_details("gamedock-none") is None
    service.deploy("sample", "One", env=ADMIN, start=True)
    d = backend.container_details("gamedock-one")
    assert d["status"] == "running" and d["ports"][0]["host_port"] == 27015 and d["restart_count"] == 0
    backend.restart_counts["gamedock-one"] = 4
    backend.unpublished_ports["gamedock-one"] = {"27015/udp"}
    d = backend.container_details("gamedock-one")
    assert d["restart_count"] == 4 and d["ports"] == [] and d["unpublished"] == ["27015/udp"]


# ------------------------------------------------------------------ diagnostics bundle
@pytest.fixture
def running(service, backend):
    service.deploy("sample", "One", env={"ADMIN_PASSWORD": "super-secret-pw", "SERVER_PASSWORD": "join-me-123"}, start=True)
    backend.set_logs("gamedock-one", "boot\nlogin password=hunter2 ok\nAuthorization: Bearer abc.def.ghi\n"
                                     "join-me-123 was typed\nserver ready\n")
    return "one"


def test_diagnostics_bundle_contents(service, running):
    d = service.get_server_diagnostics("one")
    assert d["read_only"] and d["server"]["id"] == "one" and d["server"]["status"] == "running"
    assert d["container"]["status"] == "running" and d["container"]["restart_count"] == 0
    assert d["ports"]["configured"] == [{"protocol": "udp", "host": 27015, "container": 27015}]
    assert d["ports"]["published"][0]["host_port"] == 27015 and d["gamedock"]["version"]
    assert d["backups"]["count"] == 0 and d["disk"]["free_bytes"] > 0 and "data_folder" in d
    names = {s["name"]: s for s in d["settings"]}
    assert names["SERVER_PASSWORD"] == {"name": "SERVER_PASSWORD", "secret": True, "required": False, "value": "set"}
    assert names["SERVER_NAME"]["value"] == "Sample" and names["SERVER_NAME"]["secret"] is False


def test_diagnostics_never_contains_secrets(service, running):
    d = service.get_server_diagnostics("one")
    blob = json.dumps(d) + service.diagnostics_text(d)
    for leak in ("super-secret-pw", "join-me-123", "hunter2", "abc.def.ghi"):
        assert leak not in blob
    assert "server ready" in blob and "password=" in blob      # the line is kept, the value is hidden


def test_diagnostics_text_is_readable(service, running):
    t = service.diagnostics_text(service.get_server_diagnostics("one", 20))
    assert t.startswith("GameDock diagnostics for 'One' (one)") and "Container: running" in t
    assert "Ports configured: 27015/udp->27015" in t and "SERVER_PASSWORD=set" in t and "last " in t


def test_diagnostics_of_a_crashed_server_shows_why(service, backend, running):
    service.stop("one")
    backend.exit_info["gamedock-one"] = (137, True)
    d = service.get_server_diagnostics("one")
    assert d["container"]["exit_code"] == 137 and d["container"]["out_of_memory"] is True
    assert "out of memory: yes" in service.diagnostics_text(d)


def test_diagnostics_limits_and_unknown_server(service, running):
    with pytest.raises(ValidationError):
        service.get_server_diagnostics("one", 0)
    with pytest.raises(ValidationError):
        service.get_server_diagnostics("one", 100000)
    from gamedock.errors import NotFoundError
    with pytest.raises(NotFoundError):
        service.get_server_diagnostics("nope")


def test_diagnostics_changes_nothing(service, backend, running):
    calls = [c for c in backend.calls if c[0] in ("create", "start", "stop", "remove", "pull", "exec")]
    service.get_server_diagnostics("one")
    assert [c for c in backend.calls if c[0] in ("create", "start", "stop", "remove", "pull", "exec")] == calls


def test_diagnostics_web_endpoint_and_mcp_tool(service, running):
    with TestClient(create_app(service)) as c:
        r = c.get("/api/servers/one/diagnostics?lines=10")
        assert r.status_code == 200 and r.json()["data"]["server"]["id"] == "one" and "GameDock diagnostics" in r.json()["text"]
        assert "hunter2" not in r.text and "super-secret-pw" not in r.text
        assert c.get("/api/servers/one/diagnostics?lines=0").status_code == 400
        assert c.get("/api/servers/nope/diagnostics").status_code == 404
    for mode in ("read_only", "standard", "full"):
        err, d = run(make_app(service, mode), lambda c: call(c, "get_server_diagnostics", server_id="one", log_lines=5))
        assert not err and d["server"]["id"] == "one" and "hunter2" not in json.dumps(d)


# ------------------------------------------------------------------ audit tool and page section
def test_mcp_audit_tool_and_filters(service, running):
    app = make_app(service, "standard")
    run(app, lambda c: call(c, "get_server_status", server_id="one"))
    run(app, lambda c: call(c, "get_server_status", server_id="missing"))
    run(app, lambda c: call(c, "list_servers"))
    err, r = run(app, lambda c: call(c, "get_mcp_audit", limit=10))
    assert not err and r["count"] >= 3
    assert [e["tool"] for e in r["entries"][:3]] == ["list_servers", "get_server_status", "get_server_status"]   # newest first
    err, r = run(app, lambda c: call(c, "get_mcp_audit", tool="get_server_status"))
    assert {e["tool"] for e in r["entries"]} == {"get_server_status"} and r["count"] == 2
    err, r = run(app, lambda c: call(c, "get_mcp_audit", status="error"))
    assert r["count"] >= 1 and {e["status"] for e in r["entries"]} == {"error"}
    err, msg = run(app, lambda c: call(c, "get_mcp_audit", limit=0))
    assert err


def test_audit_never_holds_secrets_or_file_bodies(service, running):
    app = make_app(service, "full")
    run(app, lambda c: call(c, "validate_catalog_entry", yaml_text="id: x\nname: SECRETBODY\nimage: a/b:1\n"))
    run(app, lambda c: call(c, "update_server_settings", server_id="one", env={"ADMIN_PASSWORD": "brand-new-pw"}))
    err, r = run(app, lambda c: call(c, "get_mcp_audit", limit=50))
    blob = json.dumps(r)
    assert "SECRETBODY" not in blob and "brand-new-pw" not in blob and "super-secret-pw" not in blob


def test_web_audit_endpoint_filters(service, running):
    run(make_app(service, "standard"), lambda c: call(c, "get_server_status", server_id="one"))
    with TestClient(create_app(service)) as c:
        assert c.get("/api/mcp/audit?limit=5").status_code == 200
        r = c.get("/api/mcp/audit?tool=get_server_status&limit=5").json()["entries"]
        assert r and {e["tool"] for e in r} == {"get_server_status"}
        assert c.get("/api/mcp/audit?status=crashed").json()["entries"] == []


# ------------------------------------------------------------------ catalog lint
def test_repository_catalog_lints_without_errors():
    report = lint_catalog(ROOT / "catalog")
    assert report.errors == [] and report.checked, report.render()


def test_lint_script_exit_codes(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir()
    (cat / "ok.yaml").write_text("id: ok\nname: Ok\nimage: a/b:1\nports:\n  - { protocol: udp, host: 4000, container: 4000 }\n"
                                 "fields:\n  - { name: A }\n")
    (cat / "bad.yaml").write_text("id: bad\nname: Bad\nimage: a/b:1\nports: nonsense\n")
    run_ = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts" / "lint_catalog.py"), *a],  # noqa: E731
                                     capture_output=True, text=True)
    r = run_(str(cat))
    assert r.returncode == 1 and "ERROR" in r.stdout and "bad.yaml" in r.stdout
    (cat / "bad.yaml").unlink()
    r = run_(str(cat))
    assert r.returncode == 0 and "0 error(s)" in r.stdout
    assert run_(str(cat), "--strict").returncode == 1       # an entry with one field earns a warning


def test_lint_flags_a_config_file_outside_every_mounted_folder(tmp_path):
    cat = tmp_path / "c"
    cat.mkdir()
    (cat / "g.yaml").write_text("id: g\nname: G\nimage: a/b:1\nvolumes:\n  - { host: ./data, container: /data }\n"
                                "config_files:\n  - { path: elsewhere/x.ini, format: ini }\ninstalled_build_file: data/version.txt\n")
    r = lint_catalog(cat)
    text = " ".join(m for _, m in r.warnings)
    assert "not inside any mounted folder" in text and "installed_build_file is set without steam_app_id" in text


# ------------------------------------------------------------------ the new tools stay safe
NEW_TOOLS = {"get_catalog_review_status", "verify_game_setup", "check_ports", "preview_deploy", "get_server_diagnostics",
             "get_mcp_audit"}


def test_new_tools_are_read_only_in_every_mode(service):
    for mode in ("read_only", "standard", "full"):
        app = make_app(service, mode)

        async def names(c):
            return {t.name: t for t in (await c.list_tools()).tools}
        got = run(app, names)
        got = got[1] if isinstance(got, tuple) else got
        assert NEW_TOOLS <= set(got)
        for n in NEW_TOOLS:
            assert got[n].annotations.read_only_hint is True


def test_no_tool_can_approve_or_rerecord_baselines(service):
    app = make_app(service, "full")

    async def names(c):
        return {t.name for t in (await c.list_tools()).tools}
    got = run(app, names)
    got = got[1] if isinstance(got, tuple) else got
    assert not [n for n in got if re.search(r"approve|reject|baseline|record_config|accept", n)]
    assert not [n for n in got if re.search(r"force|override|bypass", n)]


def test_new_web_endpoints_do_not_accept_changes(service, running):
    with TestClient(create_app(service)) as c:
        for path in ("/api/servers/one/check-setup", "/api/servers/one/diagnostics", "/api/catalog/sample/review-status"):
            assert c.post(path).status_code == 405 and c.delete(path).status_code == 405
