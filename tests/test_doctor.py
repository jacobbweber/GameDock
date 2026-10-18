import json
import socket
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from gamedock import doctor as dr
from gamedock.__main__ import doctor_main
from gamedock.backends import DockerCliBackend, FakeBackend
from gamedock.backends.base import DockerDiagnosis
from gamedock.config import Settings
from gamedock.doctor import run_doctor
from gamedock.service import GameDockService
from gamedock.web.app import create_app
from tests.conftest import ADMIN, SAMPLE


def by_id(report):
    return {c.id: c for c in report.checks}


@pytest.fixture
def settings(tmp_path, catalog_dir):
    return Settings(backend="fake", catalog_dir=catalog_dir, data_dir=tmp_path / "data")


def test_healthy_setup_with_fake_engine(service, settings):
    r = run_doctor(service, settings)
    c = by_id(r)
    assert r.ok and c["docker"].status == "info" and "FAKE" in c["docker"].message
    assert c["data_dir"].status == "ok" and c["catalog"].status == "ok" and c["bind"].status == "ok"
    assert c["mcp"].status == "ok" and "standard" in c["mcp"].message
    assert "Everything looks good" in r.summary() or "warning" in r.summary()


def test_text_report_is_plain(service, settings):
    text = run_doctor(service, settings).to_text()
    assert "[ OK ]" in text and "Game catalog" in text and "\033" not in text


# ------------------------------------------------------------------ docker, using a fake `docker` program
def fake_docker(tmp_path, version_out="", version_err="", version_rc=0, client="Docker version 27.1.1, build x"):
    script = tmp_path / "docker"
    script.write_text(f"""#!/bin/sh
if [ "$1" = "--version" ]; then echo '{client}'; exit 0; fi
if [ "$1" = "version" ]; then
  [ -n "{version_out}" ] && echo '{version_out}'
  [ -n "{version_err}" ] && echo "{version_err}" >&2
  exit {version_rc}
fi
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
@pytest.mark.parametrize("case,status,kind,fixword", [
    (dict(version_out="27.1.1"), "ok", "ok", None),
    (dict(version_err="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
          version_rc=1), "fail", "daemon_down", "systemctl enable --now docker"),
    (dict(version_err="permission denied while trying to connect to the Docker daemon socket at "
                      "unix:///var/run/docker.sock", version_rc=1), "fail", "permission", "usermod -aG docker"),
])
def test_docker_states_with_a_real_subprocess(tmp_path, catalog_dir, case, status, kind, fixword):
    backend = DockerCliBackend(docker_bin=fake_docker(tmp_path, **case))
    svc = GameDockService(backend, catalog_dir, tmp_path / "data")
    s = Settings(backend="docker", catalog_dir=catalog_dir, data_dir=tmp_path / "data")
    r = run_doctor(svc, s)
    c = by_id(r)
    assert svc.docker_status(0).kind == kind
    if kind == "ok":
        assert c["docker_daemon"].status == "ok" and "27.1.1" in c["docker_daemon"].message
        assert c["docker_access"].status == "ok"
    elif kind == "daemon_down":
        assert c["docker_binary"].status == "ok" and c["docker_daemon"].status == "fail"
        assert fixword in c["docker_daemon"].fix and not r.ok
    else:
        assert c["docker_access"].status == "fail" and fixword in c["docker_access"].fix and not r.ok


def test_docker_not_installed(tmp_path, catalog_dir):
    svc = GameDockService(DockerCliBackend(docker_bin="no-such-docker-binary-xyz"), catalog_dir, tmp_path / "data")
    r = run_doctor(svc, Settings(backend="docker", catalog_dir=catalog_dir, data_dir=tmp_path / "data"))
    c = by_id(r)
    assert c["docker_binary"].status == "fail" and "Install Docker" in c["docker_binary"].fix
    assert c["docker_daemon"].status == "info" and not r.ok


# ------------------------------------------------------------------ data folder, state, ownership, catalog
def test_data_folder_not_writable(service, settings, monkeypatch):
    import tempfile

    def deny(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", deny)
    c = by_id(run_doctor(service, settings))["data_dir"]
    assert c.status == "fail" and "not writable" in c.message and "chown" in c.fix


def test_low_disk_space(service, settings, monkeypatch):
    monkeypatch.setattr(dr, "free_space", lambda p: (100 * 1024 ** 2, 10 * 1024 ** 3))
    assert by_id(run_doctor(service, settings))["data_dir"].status == "fail"
    monkeypatch.setattr(dr, "free_space", lambda p: (3 * 1024 ** 3, 10 * 1024 ** 3))
    assert by_id(run_doctor(service, settings))["data_dir"].status == "warn"


def test_corrupt_state_file(service, settings, tmp_path):
    service.deploy("sample", "One", env=ADMIN)
    (tmp_path / "data" / "instances.json").write_text("{nope")
    c = by_id(run_doctor(service, settings))["state"]
    assert c.status == "fail" and ".bak" in c.message


def test_state_file_readable_by_others_warns(service, settings, tmp_path):
    service.deploy("sample", "One", env=ADMIN)
    (tmp_path / "data" / "instances.json").chmod(0o644)
    c = by_id(run_doctor(service, settings))["state"]
    assert c.status == "warn" and "chmod 600" in c.fix


def test_foreign_owned_files_warn(service, settings, monkeypatch):
    monkeypatch.setattr(dr, "foreign_owners", lambda root: ({0}, "/data/x/save.dat"))
    c = by_id(run_doctor(service, settings))["ownership"]
    assert c.status == "warn" and "chown" in c.fix and "PUID" in c.fix


def test_invalid_catalog_files_are_listed(service, settings, catalog_dir):
    (catalog_dir / "typo.yaml").write_text("id: typo\nname: T\n")
    c = by_id(run_doctor(service, settings))
    assert c["catalog"].status == "ok"  # the good game still works
    assert c["catalog_typo.yaml"].status == "warn" and "skipped" in c["catalog_typo.yaml"].message


def test_missing_or_empty_catalog(tmp_path):
    svc = GameDockService(FakeBackend(), tmp_path / "nope", tmp_path / "data")
    assert by_id(run_doctor(svc))["catalog"].status == "fail"
    (tmp_path / "empty").mkdir()
    svc = GameDockService(FakeBackend(), tmp_path / "empty", tmp_path / "data")
    assert by_id(run_doctor(svc))["catalog"].status == "fail"


# ------------------------------------------------------------------ ports, web, mcp
def test_port_in_use_is_found_by_trying_to_bind(service, settings):
    service.deploy("sample", "One", env=ADMIN)  # host port 27015/udp
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 27015))
    try:
        r = run_doctor(service, settings)
    finally:
        s.close()
    c = by_id(r)["port_one_udp_27015"]
    assert c.status == "fail" and "ss -lntup" in c.fix and "cannot start" in c.message and not r.ok
    assert by_id(run_doctor(service, settings))["ports"].status == "ok"  # free again


def test_running_servers_own_ports_are_not_a_problem(service, settings):
    service.deploy("sample", "One", env=ADMIN, start=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 27015))  # stands in for the container's own published port
    try:
        r = run_doctor(service, settings)
    finally:
        s.close()
    assert r.ok and not any(c.id.startswith("port_") for c in r.checks)


def test_web_and_mcp_warnings(service, tmp_path, catalog_dir):
    base = dict(backend="fake", catalog_dir=catalog_dir, data_dir=tmp_path / "data")
    c = by_id(run_doctor(service, Settings(host="0.0.0.0", **base)))
    assert c["bind"].status == "warn" and "no login" in c["bind"].message
    assert c["mcp"].status == "info" and "switched off" in c["mcp"].message  # no token on a non-local bind
    c = by_id(run_doctor(service, Settings(mcp_mode="full", **base)))
    assert c["mcp"].status == "warn" and "GAMEDOCK_MCP_TOKEN" in c["mcp"].fix
    c = by_id(run_doctor(service, Settings(mcp_mode="full", mcp_token="x" * 20, **base)))
    assert c["mcp"].status == "ok" and "token set" in c["mcp"].message and "x" * 20 not in c["mcp"].message
    c = by_id(run_doctor(service, Settings(mcp_mode="off", **base)))
    assert c["mcp"].status == "info"


# ------------------------------------------------------------------ command line and API
def test_cli_exit_codes_and_json(tmp_path, catalog_dir, monkeypatch, capsys):
    for k, v in {"GAMEDOCK_BACKEND": "fake", "GAMEDOCK_CATALOG_DIR": str(catalog_dir),
                 "GAMEDOCK_DATA_DIR": str(tmp_path / "data")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)
    assert doctor_main([]) == 0
    out = capsys.readouterr().out
    assert "GameDock doctor" in out and "[ OK ]" in out
    assert doctor_main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and {c["id"] for c in data["checks"]} >= {"docker", "data_dir", "catalog", "mcp"}
    monkeypatch.setenv("GAMEDOCK_CATALOG_DIR", str(tmp_path / "missing"))
    assert doctor_main([]) == 1  # a problem to fix -> non-zero exit
    assert "How to fix" in capsys.readouterr().out


class DownBackend(FakeBackend):
    def diagnose(self):
        return DockerDiagnosis(True, "/usr/bin/docker", None, False, None, "daemon_down", "down")


def test_status_and_doctor_api(tmp_path, catalog_dir, settings):
    svc = GameDockService(DownBackend(), catalog_dir, tmp_path / "data")
    (catalog_dir / "bad.yaml").write_text("id: bad\n")
    with TestClient(create_app(svc, settings=Settings(backend="docker", catalog_dir=catalog_dir,
                                                       data_dir=tmp_path / "data"))) as c:
        st = c.get("/api/status").json()
        assert st["docker"]["ok"] is False and st["docker"]["kind"] == "daemon_down"
        assert st["catalog_problems"][0]["file"] == "bad.yaml"
        d = c.get("/api/doctor").json()
        assert d["ok"] is False and d["counts"]["fail"] >= 1
        assert any(x["id"] == "docker_daemon" and x["status"] == "fail" and x["fix"] for x in d["checks"])
    with TestClient(create_app(GameDockService(FakeBackend(), catalog_dir, tmp_path / "d2"))) as c:
        assert c.get("/api/status").json()["docker"]["ok"] is True
        assert c.get("/api/doctor").json()["ok"] is True


def test_doctor_never_prints_secrets(service, settings, tmp_path):
    service.deploy("sample", "One", env={"ADMIN_PASSWORD": "pw-super-secret"})
    blob = json.dumps(run_doctor(service, settings).to_dict())
    assert "pw-super-secret" not in blob


def test_deploy_files_are_present_and_sane():
    import pathlib
    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    svc = compose["services"]["gamedock"]
    assert all(p.startswith("127.0.0.1:") for p in svc["ports"])  # never published to the whole network
    # data folder is mounted at the same path inside and out
    data_mount = [v for v in svc["volumes"] if "GAMEDOCK_DATA_HOST" in v][0]
    left, right = data_mount.split("}:", 1)
    assert left.split(":-")[1] == right.split(":-")[1].rstrip("}")
    for unit in ("gamedock.service", "gamedock-user.service"):
        text = (root / "deploy" / unit).read_text()
        assert "[Service]" in text and "ExecStart=" in text and "NoNewPrivileges=true" in text
        assert "TOKEN=" not in text.replace("GAMEDOCK_MCP_TOKEN", "")  # no secrets in unit files


def test_smoke_test_catalog_entry_is_marked_as_test():
    from gamedock.catalog import load_catalog
    import pathlib

    cat = load_catalog(pathlib.Path(__file__).resolve().parent.parent / "catalog")
    game = cat["smoke-test"]
    assert game.name.startswith("TEST")
    busy = {8211, 15637, 11434, 9090, 3001, 9256, 8080}
    assert not busy & {p.host for p in game.ports}
