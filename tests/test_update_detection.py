"""Image update detection against fake `docker` command output, catalog hints, and the web and MCP surfaces."""
import subprocess
import textwrap

import pytest
from fastapi.testclient import TestClient

from gamedock.backends import DockerCliBackend
from gamedock.catalog import load_catalog, parse_definition
from gamedock.errors import BackendError, CatalogError
from gamedock.web.app import create_app
from tests.conftest import ADMIN
from tests.mcp_helpers import call, make_app, run
from tests.test_docker_cli import MISSING, OURS, Recorder

D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64


# ------------------------------------------------------------------ docker command output
def test_remote_digest_from_buildx_format():
    rec = Recorder([(0, D1 + "\n", "")])
    assert DockerCliBackend(runner=rec).remote_image_digest("img:1") == D1
    assert rec.argvs()[0][:4] == ["docker", "buildx", "imagetools", "inspect"]
    assert "pull" not in [a for argv in rec.argvs() for a in argv]          # never downloads


def test_remote_digest_falls_back_to_plain_buildx_output():
    out = f"Name:      docker.io/library/img:1\nMediaType: application/vnd.oci.image.index.v1+json\nDigest:    {D2}\n"
    rec = Recorder([(1, "", "unknown flag: --format"), (0, out, "")])
    assert DockerCliBackend(runner=rec).remote_image_digest("img:1") == D2


def test_remote_digest_falls_back_to_manifest_inspect_for_single_platform():
    rec = Recorder([(1, "", "no buildx"), (1, "", "no buildx"),
                    (0, '{"Descriptor": {"digest": "%s"}}' % D1, "")])
    assert DockerCliBackend(runner=rec).remote_image_digest("img:1") == D1


def test_remote_digest_unknown_for_multi_platform_manifest_inspect():
    rec = Recorder([(1, "", "x"), (1, "", "x"), (0, '[{"Descriptor": {"digest": "%s"}}]' % D1, "")])
    assert DockerCliBackend(runner=rec).remote_image_digest("img:1") is None


def test_remote_digest_failure_is_a_plain_error():
    rec = Recorder([(1, "", "Get https://registry-1.docker.io/v2/: dial tcp: lookup registry: no such host"), (1, "", "e"), (1, "", "e")])
    with pytest.raises(BackendError, match="Could not ask the registry about img:1"):
        DockerCliBackend(runner=rec).remote_image_digest("img:1")


def test_remote_digest_refuses_odd_image_names():
    with pytest.raises(BackendError):
        DockerCliBackend(runner=Recorder([])).remote_image_digest("--help")


def test_local_digests_parse_repo_digests():
    out = '["thijsvanloef/palworld-server-docker@%s","mirror.example/pal@%s"]\n' % (D1, D2)
    rec = Recorder([(0, out, "")])
    assert DockerCliBackend(runner=rec).local_image_digests("sha256:" + "c" * 64) == (D1, D2)
    assert rec.argvs()[0][:3] == ["docker", "image", "inspect"]


@pytest.mark.parametrize("res", [(1, "", "No such image"), (0, "null\n", ""), (0, "[]\n", ""), (0, "garbage", "")])
def test_local_digests_empty_when_unknown(res):
    assert DockerCliBackend(runner=Recorder([res])).local_image_digests("img:1") == ()


def test_exec_runs_without_a_shell_in_our_running_container():
    rec = Recorder([(0, OURS, ""), (0, "Saved\n", "")])
    out = DockerCliBackend(runner=rec).exec_in("gamedock-x", ["rcon-cli", "save; rm -rf /"])
    assert out == "Saved\n"
    assert rec.argvs()[1] == ["docker", "exec", "gamedock-x", "rcon-cli", "save; rm -rf /"]


def test_exec_refuses_stopped_foreign_and_failing():
    with pytest.raises(BackendError, match="not running"):
        DockerCliBackend(runner=Recorder([(0, "exited|0|false|true\n", "")])).exec_in("gamedock-x", ["a"])
    with pytest.raises(BackendError):
        DockerCliBackend(runner=Recorder([(0, "running|0|false|\n", "")])).exec_in("gamedock-x", ["a"])
    with pytest.raises(BackendError, match="status 3"):
        DockerCliBackend(runner=Recorder([(0, OURS, ""), (3, "boom", "")])).exec_in("gamedock-x", ["a"])


# ------------------------------------------------------------------ catalog fields
def entry(**extra):
    return {"id": "g", "name": "G", "image": "x:1", **extra}


def test_new_optional_fields_parse_and_round_trip():
    from gamedock.catalog import entry_to_dict
    e = parse_definition(entry(update_note=" Updates on start. ", backup_stop_first=True, save_command="rcon-cli save"))
    assert e.update_note == "Updates on start." and e.backup_stop_first is True and e.save_command == ("rcon-cli", "save")
    assert parse_definition(entry_to_dict(e)) == e
    d = parse_definition(entry())
    assert d.update_note == "" and d.backup_stop_first is False and d.save_command == ()


@pytest.mark.parametrize("extra", [dict(backup_stop_first="yes"), dict(save_command=[1, 2]), dict(save_command=["a"] * 20),
                                   dict(save_command=["a\nb"]), dict(save_command={"a": 1}), dict(save_command="'unclosed")])
def test_bad_new_fields_are_refused(extra):
    with pytest.raises(CatalogError):
        parse_definition(entry(**extra))


def test_shipped_palworld_describes_how_it_updates():
    from pathlib import Path
    defs = load_catalog(Path(__file__).parent.parent / "catalog")
    assert defs["palworld"].update_note
    assert defs["palworld"].save_command


# ------------------------------------------------------------------ web
@pytest.fixture
def client(service):
    with TestClient(create_app(service)) as c:
        yield c


def test_web_image_update_endpoints(client, service, backend, deployed):
    s = client.get(f"/api/servers/{deployed}").json()
    assert s["update_available"] is None and "schedule" in s
    r = client.post(f"/api/servers/{deployed}/image-update/check").json()
    assert r["update_available"] is False
    backend.publish_new_image("busybox:latest")
    service._image_checks[deployed].checked_at -= 100
    assert client.post(f"/api/servers/{deployed}/image-update/check").json()["update_available"] is True
    assert client.get(f"/api/servers/{deployed}/image-update").json()["update_available"] is True
    assert client.get(f"/api/servers/{deployed}").json()["update_available"] is True
    assert [x for x in client.get("/api/servers").json() if x["id"] == deployed][0]["update_available"] is True
    assert client.get("/api/servers/nope/image-update").status_code == 404


def test_web_schedule_endpoints(client, service, deployed):
    r = client.put(f"/api/servers/{deployed}/schedules/backup",
                   json={"enabled": True, "frequency": "weekly", "time": "03:30", "days": ["sun", "wed"], "retention": 4})
    assert r.status_code == 200 and r.json()["backup"]["summary"] == "Wednesday, Sunday at 03:30"
    b = client.get(f"/api/servers/{deployed}/schedules").json()["backup"]
    assert b["days"] == ["wed", "sun"] and b["time"] == "03:30" and b["retention"] == 4
    m = client.put(f"/api/servers/{deployed}/schedules/maintenance",
                   json={"enabled": True, "frequency": "daily", "time": "04:00", "backup_first": True, "update_image": False,
                         "restart": True})
    assert m.json()["maintenance"]["summary"] == "daily at 04:00"
    allv = client.get("/api/schedules").json()
    assert allv["timezone"]["name"] and allv["servers"][0]["server_id"] == deployed
    assert client.get(f"/api/servers/{deployed}").json()["schedule"]["maintenance"] == "daily at 04:00"


@pytest.mark.parametrize("body", [{"enabled": "yes"}, {"enabled": True, "frequency": "hourly"},
                                  {"enabled": True, "frequency": "interval", "every_hours": "6"},
                                  {"enabled": True, "frequency": "interval", "every_hours": 0},
                                  {"enabled": True, "frequency": "daily", "time": "7pm"},
                                  {"enabled": True, "frequency": "weekly", "time": "07:00", "days": []},
                                  {"enabled": True, "frequency": "daily", "time": "07:00", "retention": 0}])
def test_web_rejects_bad_backup_schedules(client, deployed, body):
    r = client.put(f"/api/servers/{deployed}/schedules/backup", json=body)
    assert r.status_code in (400, 422) and "error" in r.json()


def test_web_run_now_needs_confirm(client, deployed):
    assert client.post(f"/api/servers/{deployed}/schedules/backup/run").status_code == 400
    r = client.post(f"/api/servers/{deployed}/schedules/backup/run?confirm=true")
    assert r.status_code == 200 and r.json()["result"] in ("ok", "skipped")
    assert client.post(f"/api/servers/{deployed}/schedules/bogus/run?confirm=true").status_code == 400


def test_web_run_now_busy_is_409(client, service, deployed):
    with service._operation(deployed, "updating"):
        assert client.post(f"/api/servers/{deployed}/schedules/backup/run?confirm=true").status_code == 409


def test_static_files_are_revalidated(client):
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/").headers["cache-control"] == "no-cache"


# ------------------------------------------------------------------ MCP
def test_mcp_version_and_schedule_tools(service, deployed):
    async def go(c):
        v = await call(c, "get_gamedock_version")
        assert not v[0] and v[1]["version"] == "0.6.1" and v[1]["update_available"] is None
        assert "install_dir" not in v[1] and "manual_commands" not in str(v[1])
        r = await call(c, "set_backup_schedule", server_id=deployed, enabled=True, frequency="daily", time="02:00", retention=3)
        assert not r[0] and r[1]["backup"]["summary"] == "daily at 02:00"
        m = await call(c, "set_maintenance_schedule", server_id=deployed, enabled=True, frequency="weekly", time="05:00",
                       days=["sun"], update_image=False)
        assert not m[0] and m[1]["maintenance"]["summary"] == "Sunday at 05:00" and m[1]["maintenance"]["update_image"] is False
        lst = await call(c, "list_schedules")
        assert lst[1]["servers"][0]["backup"]["retention"] == 3 and lst[1]["timezone"]["name"]
        st = await call(c, "get_server_status", server_id=deployed)
        assert "update_available" in st[1] and st[1]["schedule"]["backup"] == "daily at 02:00"
        return True

    assert run(make_app(service, "standard"), go)


def test_mcp_schedule_inputs_are_bounded(service, deployed):
    async def go(c):
        bad = [dict(frequency="daily", time="25:00"), dict(frequency="interval", every_hours=0),
               dict(frequency="interval", every_hours=999), dict(frequency="daily", time="03:00", retention=500),
               dict(frequency="weekly", time="03:00", days=["funday"]), dict(frequency="monthly")]
        for kw in bad:
            r = await call(c, "set_backup_schedule", server_id=deployed, enabled=True, **kw)
            assert r[0], kw
        r = await call(c, "set_backup_schedule", server_id=deployed, enabled=True)   # no frequency the first time
        assert r[0] and "how often" in r[1]
        return True

    assert run(make_app(service, "standard"), go)


def test_mcp_read_only_can_read_but_not_set(service, deployed):
    async def go(c):
        assert not (await call(c, "list_schedules"))[0]
        assert not (await call(c, "get_gamedock_version"))[0]
        r = await call(c, "set_backup_schedule", server_id=deployed, enabled=True, frequency="daily", time="03:00")
        assert r[0] and "Unknown tool" in r[1]
        return True

    assert run(make_app(service, "read_only"), go)


def test_mcp_tool_counts(service):
    async def go(c):
        return len((await c.list_tools()).tools)

    assert run(make_app(service, "read_only"), go) == 24
    assert run(make_app(service, "standard"), go) == 37
    assert run(make_app(service, "full"), go) == 41


def test_mcp_guide_mentions_updates_and_schedules(service):
    async def go(c):
        r = await c.read_resource("gamedock://guide")
        return r.contents[0].text

    text = run(make_app(service, "standard"), go)
    assert "list_schedules" in text and "set_maintenance_schedule" in text and "update_available" in text
    assert "get_gamedock_version" in text and "only a person" in text
