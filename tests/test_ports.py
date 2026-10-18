"""0.6.0: host port conflict checks (pure logic, service, deploy messages, web API, MCP tool, validate warnings)."""
import textwrap

import pytest
from fastapi.testclient import TestClient

from gamedock import portcheck as pc
from gamedock.backends import FakeBackend
from gamedock.errors import ConflictError, ValidationError
from gamedock.models import PortMapping
from gamedock.service import GameDockService
from gamedock.web.app import create_app
from tests.conftest import ADMIN, SAMPLE
from tests.mcp_helpers import call, make_app, run

OTHER = textwrap.dedent("""\
    id: other
    name: Other Game
    image: busybox:1
    description: Another game with the same default port.
    notes: Test.
    ports:
      - { protocol: udp, host: 27015, container: 7777 }
      - { protocol: tcp, host: 27016, container: 7778 }
    fields:
      - { name: SERVER_NAME, default: "x" }
    """)


def ports(*pairs):
    return [pc.Wanted(p, h) for p, h in pairs]


# ------------------------------------------------------------------ pure logic
def test_free_ports_have_no_findings():
    r = pc.find_conflicts(ports(("udp", 30000)), servers=[], catalog_defaults=[], probe=lambda p, h: True)
    assert r.ok and r.findings == [] and "look free" in r.to_dict(ports(("udp", 30000)))["summary"]


def test_gamedock_server_conflict_with_suggestion():
    w = ports(("udp", 27015))
    r = pc.find_conflicts(w, servers=[("a", "Server A", {("udp", 27015)}, False)], catalog_defaults=[], probe=None)
    assert not r.ok
    f = r.findings[0]
    assert f.kind == "gamedock_server" and f.level == pc.CONFLICT and "Server A" in f.message and f.suggestion == 27016
    assert r.to_dict(w)["host_check_available"] is False and "could not be checked" in r.to_dict(w)["summary"]


def test_protocol_matters():
    r = pc.find_conflicts(ports(("tcp", 27015)), servers=[("a", "A", {("udp", 27015)}, True)], catalog_defaults=[], probe=None)
    assert r.ok and r.findings == []


def test_host_listener_conflict_and_own_running_exception():
    probe = lambda proto, port: False if port in (8211, 8212) else True  # noqa: E731
    r = pc.find_conflicts(ports(("udp", 8211)), servers=[], catalog_defaults=[], probe=probe)
    assert r.findings[0].kind == "host_listener" and "ss -lntup" in r.findings[0].message
    assert r.findings[0].suggestion == 8213    # 8212 is also taken by the program
    own = pc.find_conflicts(ports(("udp", 8211)), servers=[], catalog_defaults=[], probe=probe, own_running={("udp", 8211)})
    assert own.ok and own.findings == []


def test_catalog_default_clash_is_info_not_conflict():
    r = pc.find_conflicts(ports(("udp", 27015)), servers=[], probe=lambda p, h: True,
                          catalog_defaults=[("pal", "Palworld", {("udp", 27015)})])
    f = r.findings[0]
    assert r.ok and f.kind == "catalog_default" and f.level == pc.INFO and "Palworld" in f.message and f.suggestion == 27016


def test_duplicate_and_invalid_ports():
    r = pc.find_conflicts(ports(("udp", 9000), ("udp", 9000), ("tcp", 70000)), servers=[], catalog_defaults=[], probe=None)
    assert [f.kind for f in r.findings] == ["duplicate", "invalid"] and not r.ok


def test_suggest_free_skips_taken_and_stays_in_range():
    assert pc.suggest_free("udp", 100, lambda p, n: n in (101, 102)) == 103
    assert pc.suggest_free("udp", 65535, lambda p, n: n > 65500) is not None   # falls back to lower numbers
    assert pc.suggest_free("udp", 5000, lambda p, n: True) is None


def test_default_collisions_helper():
    out = pc.default_collisions("me", [("udp", 27015), ("tcp", 1)], [("pal", "Palworld", {("udp", 27015)}), ("me", "Me", {("tcp", 1)})])
    assert out == [("udp", 27015, "pal", "Palworld")]


# ------------------------------------------------------------------ service
@pytest.fixture
def two_games(catalog_dir):
    (catalog_dir / "other.yaml").write_text(OTHER)
    return catalog_dir


@pytest.fixture
def svc(backend, two_games, tmp_path, clock):
    return GameDockService(backend, two_games, tmp_path / "data", clock=clock)


def test_check_ports_reports_other_servers_and_catalog_defaults(svc):
    svc.deploy("sample", "One", env=ADMIN)                                   # uses 27015/udp
    r = svc.check_ports([("udp", 27015), ("udp", 9999)], game_id="other")
    kinds = {(f["protocol"], f["host"]): f["kind"] for f in r["findings"]}
    assert kinds == {("udp", 27015): "gamedock_server"} and r["ok"] is False and r["read_only"] is True
    assert r["ports"][1]["status"] == "free"
    assert r["findings"][0]["suggested_host_port"] == 27016


def test_check_ports_catalog_default_between_games(svc):
    r = svc.check_ports([("udp", 27015)], game_id="other")                   # 'sample' also defaults to 27015/udp
    assert r["ok"] and r["findings"][0]["kind"] == "catalog_default" and "Sample Game" in r["findings"][0]["message"]
    assert svc.check_ports([("udp", 27015)], game_id="sample")["findings"][0]["with"].startswith("Other Game")


def test_check_ports_ignores_the_server_being_edited(svc):
    svc.deploy("sample", "One", env=ADMIN)
    r = svc.check_ports([("udp", 27015)], game_id="sample", ignore_server_id="one")
    assert [f["kind"] for f in r["findings"]] == ["catalog_default"]          # not a clash with itself


def test_check_ports_uses_host_probe(backend, two_games, tmp_path):
    taken = {("udp", 30001)}
    s = GameDockService(backend, two_games, tmp_path / "d", port_probe=lambda p, h: False if (p, h) in taken else True)
    r = s.check_ports([("udp", 30001), ("udp", 30002)])
    assert r["host_check_available"] and r["findings"][0]["kind"] == "host_listener"
    assert r["findings"][0]["suggested_host_port"] == 30003    # 30002 is one of the ports being asked about


def test_check_ports_rejects_bad_input(svc):
    with pytest.raises(ValidationError):
        svc.check_ports([])
    with pytest.raises(ValidationError):
        svc.check_ports([("icmp", 1)])
    with pytest.raises(ValidationError):
        svc.check_ports([("udp", p) for p in range(2000, 2070)])


def test_check_ports_does_not_change_anything(svc, backend):
    before = list(backend.calls)
    svc.check_ports([("udp", 27015)])
    assert [c for c in backend.calls if c[0] in ("create", "start", "stop", "remove", "pull")] == [
        c for c in before if c[0] in ("create", "start", "stop", "remove", "pull")]
    assert svc.store.all() == []


def test_deploy_port_conflict_message_is_plain_and_suggests_a_port(svc):
    svc.deploy("sample", "One", env=ADMIN)
    with pytest.raises(ConflictError) as e:
        svc.deploy("other", "Two", ports=[PortMapping("udp", 27015, 7777)])
    msg = str(e.value)
    assert "27015/udp" in msg and "already used by server 'One'" in msg and "choose a different host port" in msg
    assert "For example 27016 is free" in msg


def test_deploy_start_blocked_by_host_program_suggests_a_port(backend, two_games, tmp_path):
    s = GameDockService(backend, two_games, tmp_path / "d", port_probe=lambda p, h: False if h == 27015 else True)
    with pytest.raises(ConflictError) as e:
        s.deploy("sample", "One", env=ADMIN, start=True)
    assert "already in use on this machine" in str(e.value) and "For example 27016 is free" in str(e.value)


def test_missing_required_field_message(svc):
    with pytest.raises(ValidationError) as e:
        svc.deploy("sample", "One", env={})
    assert "The setting 'Admin password' (ADMIN_PASSWORD) is required" in str(e.value)


def test_unknown_setting_and_wrong_container_port_messages(svc):
    with pytest.raises(ValidationError) as e:
        svc.deploy("sample", "One", env={**ADMIN, "NOPE": "1"})
    assert "Unknown setting 'NOPE'" in str(e.value) and "SERVER_NAME" in str(e.value)
    with pytest.raises(ValidationError) as e:
        svc.deploy("sample", "One", env=ADMIN, ports=[PortMapping("udp", 1234, 9999)])
    assert "has no container port 9999/udp" in str(e.value) and "27015/udp" in str(e.value)


def test_image_pull_failure_message_is_plain(svc, backend):
    backend.fail_next = "create"
    with pytest.raises(Exception) as e:
        svc.deploy("sample", "One", env=ADMIN)
    msg = str(e.value)
    assert "Could not create the server 'One'" in msg and "busybox:latest" in msg and "nothing to clean up" in msg
    assert svc.store.all() == []


# ------------------------------------------------------------------ web API and MCP tool
def test_web_port_check_endpoint(svc):
    svc.deploy("sample", "One", env=ADMIN)
    with TestClient(create_app(svc)) as c:
        r = c.post("/api/ports/check", json={"ports": [{"protocol": "udp", "host": 27015}], "game_id": "other"})
        assert r.status_code == 200 and r.json()["ok"] is False
        assert r.json()["findings"][0]["suggested_host_port"] is not None
        assert c.post("/api/ports/check", json={"ports": [{"protocol": "icmp", "host": 1}]}).status_code == 422
        assert c.post("/api/ports/check", json={"ports": []}).status_code == 400


def test_mcp_check_ports_tool_in_every_mode(svc):
    svc.deploy("sample", "One", env=ADMIN)
    for mode in ("read_only", "standard", "full"):
        async def go(c):
            return await call(c, "check_ports", ports=[{"protocol": "udp", "host": 27015}], game_id="other")

        err, r = run(make_app(svc, mode), go)
        assert not err and r["ok"] is False and r["findings"][0]["kind"] == "gamedock_server"

    async def bad(c):
        return await call(c, "check_ports", ports=[{"protocol": "udp", "host": 0}])

    assert run(make_app(svc, "full"), bad)[0] is True
