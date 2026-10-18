import json
import logging

import pytest
from fastapi.testclient import TestClient

from gamedock.config import Settings
from gamedock.mcp_api import McpConfig
from gamedock.mcp_api.audit import AuditLog, mask_args
from gamedock.mcp_api.confirm import ConfirmationError, ConfirmationStore
from gamedock.web.app import create_app
from tests.conftest import ADMIN
from tests.mcp_helpers import call, make_app, run

READ = {"list_catalog", "get_catalog_entry", "list_servers", "get_server_status", "get_server_stats",
        "get_host_stats", "get_stats_history", "list_backups", "list_config_files", "read_config_file", "get_server_logs"}
STANDARD = {"deploy_server", "start_server", "stop_server", "restart_server", "update_server_settings",
            "update_server_image", "create_backup", "write_config_file"}
DESTRUCTIVE = {"delete_server", "restore_backup", "delete_backup", "wipe_server_data"}
SECRET = "sup3r-s3cret-pw"
RPC = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class Tick:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def names(c):
    return {t.name for t in (await c.list_tools()).tools}


# ------------------------------------------------------------------ modes
@pytest.mark.parametrize("mode,expected", [
    ("read_only", READ), ("standard", READ | STANDARD), ("full", READ | STANDARD | DESTRUCTIVE)])
def test_tool_listing_per_mode(service, mode, expected):
    assert run(make_app(service, mode), names) == expected


def test_off_serves_nothing(service):
    with TestClient(make_app(service, "off")) as c:
        r = c.post("/mcp", json=RPC, headers=HDR)
        assert r.status_code == 404 and "switched off" in r.json()["error"]
        assert c.get("/api/mcp/info").json()["enabled"] is False
    with TestClient(create_app(service)) as c:  # no MCP settings at all: also off
        assert c.post("/mcp", json=RPC, headers=HDR).status_code == 404


def test_hidden_tools_cannot_be_called(service):
    async def go(c):
        return await c.call_tool("delete_server", {"server_id": "x"})

    r = run(make_app(service, "standard"), go)
    assert r.is_error and "Unknown tool" in r.content[0].text


def test_default_mode_rules():
    assert Settings().effective_mcp_mode() == ("standard", "")  # localhost, no token: on
    assert Settings(host="0.0.0.0").effective_mcp_mode()[0] == "off"
    assert "MCP_TOKEN" in Settings(host="192.168.1.5").effective_mcp_mode()[1]
    assert Settings(host="0.0.0.0", mcp_token="t").effective_mcp_mode() == ("standard", "")
    assert Settings(host="0.0.0.0", mcp_mode="off", mcp_token="t").effective_mcp_mode()[0] == "off"
    assert Settings(host="localhost", mcp_mode="full").effective_mcp_mode()[0] == "full"


def test_mode_from_env(monkeypatch):
    monkeypatch.setenv("GAMEDOCK_MCP_MODE", "Read-Only")
    assert Settings.from_env().mcp_mode == "read_only"
    monkeypatch.setenv("GAMEDOCK_MCP_MODE", "banana")
    with pytest.raises(ValueError, match="GAMEDOCK_MCP_MODE"):
        Settings.from_env()


def test_token_not_in_settings_repr():
    assert "abc123tok" not in repr(Settings(mcp_token="abc123tok"))
    assert "abc123tok" not in repr(McpConfig(token="abc123tok"))


# ------------------------------------------------------------------ auth, host, origin
def post(app, headers=None, **kw):
    with TestClient(app, base_url=kw.pop("base_url", "http://127.0.0.1:8080")) as c:
        return c.post("/mcp", json=RPC, headers={**HDR, **(headers or {})})


def test_no_token_configured_localhost_works(service):
    assert post(make_app(service, "standard")).status_code == 200


def test_bearer_token_required(service):
    app = make_app(service, "standard", token="tok-123")
    r = post(app)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert post(app, {"Authorization": "Bearer nope"}).status_code == 401
    assert post(app, {"Authorization": "Basic tok-123"}).status_code == 401
    assert post(app, {"Authorization": "bearer tok-123"}).status_code == 200  # scheme is case-insensitive
    assert post(app, {"Authorization": "Bearer tok-123"}).status_code == 200


def test_token_compare_is_constant_time(monkeypatch, service):
    import hmac
    seen = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: seen.append(1) or real(a, b))
    post(make_app(service, "standard", token="tok"), {"Authorization": "Bearer x"})
    assert seen


def test_sdk_client_with_and_without_token(service):
    app = make_app(service, "read_only", token="tok-123")
    assert run(app, names, headers={"Authorization": "Bearer tok-123"}) == READ
    with pytest.raises(BaseException):
        run(app, names)
    with pytest.raises(BaseException):
        run(app, names, headers={"Authorization": "Bearer wrong"})


def test_host_header_checked(service):
    app = make_app(service, "standard")
    assert post(app, base_url="http://localhost:9999").status_code == 200
    assert post(app, base_url="http://[::1]:8080").status_code == 200
    r = post(app, base_url="http://evil.example:8080")
    assert r.status_code == 421
    assert post(app, base_url="http://127.0.0.1.evil.example").status_code == 421


def test_extra_allowed_hosts_and_wildcard_bind(service):
    app = make_app(service, "standard", token="t", bind_host="0.0.0.0")
    auth = {"Authorization": "Bearer t"}
    assert post(app, auth, base_url="http://192.168.1.5:8080").status_code == 200  # nothing configured: token only
    app = make_app(service, "standard", token="t", bind_host="0.0.0.0", allowed_hosts=("gamedock.lan:8080",))
    assert post(app, auth, base_url="http://gamedock.lan:8080").status_code == 200
    assert post(app, auth, base_url="http://192.168.1.5:8080").status_code == 421
    app = make_app(service, "standard", token="t", bind_host="192.168.1.5")
    assert post(app, auth, base_url="http://192.168.1.5:8080").status_code == 200
    assert post(app, auth, base_url="http://192.168.1.6:8080").status_code == 421


def test_origin_checked(service):
    app = make_app(service, "standard")
    assert post(app, {"Origin": "https://evil.example"}).status_code == 403
    assert post(app, {"Origin": "null"}).status_code == 403
    assert post(app, {"Origin": "http://127.0.0.1:8080"}).status_code == 200  # same origin
    app = make_app(service, "standard", allowed_origins=("http://localhost:6274",))
    assert post(app, {"Origin": "http://localhost:6274"}).status_code == 200
    assert post(app, {"Origin": "http://localhost:6275"}).status_code == 403


def test_web_csrf_still_applies_elsewhere(service):
    with TestClient(make_app(service, "standard")) as c:
        r = c.post("/api/servers", json={"game_id": "sample", "name": "x"},
                   headers={"Origin": "https://evil.example"})
        assert r.status_code == 403


def test_both_url_spellings_work_without_redirect(service):
    with TestClient(make_app(service, "standard"), base_url="http://127.0.0.1:8080",
                    follow_redirects=False) as c:
        assert c.post("/mcp", json=RPC, headers=HDR).status_code == 200
        assert c.post("/mcp/", json=RPC, headers=HDR).status_code == 200


# ------------------------------------------------------------------ confirmation store
def test_confirmation_token_single_use_and_bound():
    clock = Tick()
    s = ConfirmationStore(ttl=120, clock=clock)
    args = {"server_id": "one", "delete_data": True}
    t = s.issue("delete_server", args)
    s.consume(t, "delete_server", dict(args))
    with pytest.raises(ConfirmationError, match="unknown, already used"):
        s.consume(t, "delete_server", args)  # reuse

    t = s.issue("delete_server", args)
    with pytest.raises(ConfirmationError, match="different arguments"):
        s.consume(t, "delete_server", {**args, "delete_data": False})
    with pytest.raises(ConfirmationError):  # a mismatch burns the token
        s.consume(t, "delete_server", args)

    t = s.issue("delete_server", args)
    with pytest.raises(ConfirmationError, match="different arguments"):
        s.consume(t, "wipe_server_data", args)  # different action

    t = s.issue("delete_server", args)
    clock.t += 119
    s.consume(t, "delete_server", args)  # still fine just before expiry
    t = s.issue("delete_server", args)
    clock.t += 121
    with pytest.raises(ConfirmationError, match="expired|unknown"):
        s.consume(t, "delete_server", args)
    with pytest.raises(ConfirmationError):
        s.consume("made-up", "delete_server", args)


def test_confirmation_store_is_bounded():
    s = ConfirmationStore()
    for i in range(500):
        s.issue("a", {"i": i})
    assert len(s._pending) <= 200


# ------------------------------------------------------------------ the two-step flow through MCP
def deploy_args(**extra):
    return {"game_id": "sample", "name": "One", "env": dict(ADMIN), **extra}


def test_delete_server_two_step(service):
    async def go(c):
        err, d = await call(c, "deploy_server", **deploy_args())
        assert not err and d["id"] == "one"
        err, first = await call(c, "delete_server", server_id="one")
        assert not err and first["status"] == "confirmation_required"
        assert "KEPT" in first["summary"] and first["confirmation_token"] and first["expires_in_seconds"] == 120
        assert "one" in [s.id for s in service.list_servers()]  # nothing happened yet
        err, done = await call(c, "delete_server", server_id="one", confirmation_token=first["confirmation_token"])
        assert not err and done["status"] == "done" and done["data_kept"] is True
        assert service.list_servers() == []
        err, msg = await call(c, "delete_server", server_id="one", confirmation_token=first["confirmation_token"])
        assert err and "already used" in msg or "unknown" in msg  # token reuse refused

    run(make_app(service), go)


def test_delete_with_data_summary_and_backup(service, tmp_path):
    async def go(c):
        await call(c, "deploy_server", **deploy_args())
        (tmp_path / "data" / "one" / "saves" / "w.txt").write_text("world")
        err, first = await call(c, "delete_server", server_id="one", delete_data=True)
        s = first["summary"]
        assert "ALSO be deleted" in s and "automatic backup" in s and "1 files" in s
        err, done = await call(c, "delete_server", server_id="one", delete_data=True,
                               confirmation_token=first["confirmation_token"])
        assert not err and done["data_deleted"] and done["backup"]
        assert not (tmp_path / "data" / "one").exists()

    run(make_app(service), go)


def test_token_for_different_arguments_is_refused(service):
    async def go(c):
        await call(c, "deploy_server", **deploy_args())
        err, first = await call(c, "delete_server", server_id="one", delete_data=False)
        tok = first["confirmation_token"]
        err, msg = await call(c, "delete_server", server_id="one", delete_data=True, confirmation_token=tok)
        assert err and "different arguments" in msg
        assert [s.id for s in service.list_servers()] == ["one"]
        _, second = await call(c, "delete_server", server_id="one", delete_data=False)
        err, msg = await call(c, "wipe_server_data", server_id="one", confirmation_token=second["confirmation_token"])
        assert err and "different arguments" in msg  # token from another action

    run(make_app(service), go)


def test_token_expiry_through_mcp(service):
    clock = Tick()
    store = ConfirmationStore(ttl=120, clock=clock)

    async def go(c):
        await call(c, "deploy_server", **deploy_args())
        _, first = await call(c, "delete_server", server_id="one")
        clock.t += 130
        err, msg = await call(c, "delete_server", server_id="one", confirmation_token=first["confirmation_token"])
        assert err and ("expired" in msg or "unknown" in msg)
        assert [s.id for s in service.list_servers()] == ["one"]

    run(make_app(service, confirmations=store), go)


def test_running_server_is_refused_before_a_token_is_issued(service):
    async def go(c):
        await call(c, "deploy_server", **deploy_args(start=True))
        for tool in ("delete_server", "wipe_server_data"):
            err, msg = await call(c, tool, server_id="one")
            assert err and "Stop it first" in msg

    store = ConfirmationStore()
    run(make_app(service, confirmations=store), go)
    assert len(store._pending) == 0


def test_wipe_restore_delete_backup_flows(service, tmp_path):
    saves = tmp_path / "data" / "one" / "saves"

    async def go(c):
        await call(c, "deploy_server", **deploy_args())
        (saves / "a.txt").write_text("v1")
        err, b = await call(c, "create_backup", server_id="one")
        assert not err
        name = b["backup"]["name"]
        (saves / "a.txt").write_text("v2")

        # restore
        err, first = await call(c, "restore_backup", server_id="one", backup_name=name)
        assert name in first["summary"] and "pre-restore" in first["summary"] and "replaced" in first["summary"]
        assert (saves / "a.txt").read_text() == "v2"
        err, done = await call(c, "restore_backup", server_id="one", backup_name=name,
                               confirmation_token=first["confirmation_token"])
        assert not err and done["status"] == "done" and (saves / "a.txt").read_text() == "v1"

        # wipe
        err, first = await call(c, "wipe_server_data", server_id="one")
        assert "pre-wipe" in first["summary"] and (saves / "a.txt").exists()
        err, done = await call(c, "wipe_server_data", server_id="one", confirmation_token=first["confirmation_token"])
        assert not err and not (saves / "a.txt").exists()

        # delete a backup
        err, first = await call(c, "delete_backup", server_id="one", backup_name=name)
        assert "cannot be undone" in first["summary"]
        err, done = await call(c, "delete_backup", server_id="one", backup_name=name,
                               confirmation_token=first["confirmation_token"])
        assert not err and done["deleted"] == name
        assert name not in [b.name for b in service.list_backups("one")]

        err, msg = await call(c, "restore_backup", server_id="one", backup_name=name)
        assert err and "no backup named" in msg

    run(make_app(service), go)


# ------------------------------------------------------------------ secrets
def test_secrets_never_returned_or_stored_in_audit(service, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)  # (the SDK *client* logs what it sends; only server-side loggers matter)
    env = {"ADMIN_PASSWORD": SECRET, "SERVER_PASSWORD": SECRET + "-2", "SERVER_NAME": "Visible"}

    async def go(c):
        outputs = []
        _, d = await call(c, "deploy_server", game_id="sample", name="One", env=env)
        outputs.append(d)
        outputs.append(await call(c, "list_servers"))
        outputs.append(await call(c, "get_server_status", server_id="one"))
        outputs.append(await call(c, "update_server_settings", server_id="one", env={"SERVER_PASSWORD": SECRET + "-3"}))
        outputs.append(await call(c, "list_catalog"))
        outputs.append(await call(c, "get_catalog_entry", game_id="sample"))
        # a config file holding a secret value is masked on read, and the write is not logged
        (tmp_path / "data" / "one" / "conf").mkdir(parents=True, exist_ok=True)
        await call(c, "write_config_file", server_id="one", path="conf/settings.ini",
                   content=f"Name=x\nAdminPassword={SECRET}-cfg\n")
        outputs.append(await call(c, "read_config_file", server_id="one", path="conf/settings.ini"))
        outputs.append(await call(c, "deploy_server", game_id="sample", name="Bad", env={"ADMIN_PASSWORD": SECRET},
                                  ports=[{"protocol": "udp", "container": 27015, "host": 27015}]))  # port clash
        blob = json.dumps(outputs)
        assert SECRET not in blob
        assert "Visible" in blob  # normal values are shown
        _, status = await call(c, "get_server_status", server_id="one")
        pw = [e for e in status["env"] if e["name"] == "ADMIN_PASSWORD"][0]
        assert pw["is_set"] is True and pw["value"] is None

    run(make_app(service), go)
    audit_text = (tmp_path / "data" / "mcp-audit.jsonl").read_text()
    assert SECRET not in audit_text
    server_logs = "\n".join(r.getMessage() for r in caplog.records
                            if r.name.startswith(("gamedock", "mcp.server")))
    assert server_logs and SECRET not in server_logs
    assert "Visible" in audit_text  # non-secret settings are logged
    assert "***" in audit_text


def test_mask_args_unit():
    out = mask_args({"env": {"MY_CUSTOM": "pw1", "OTHER": "ok"}, "confirmation_token": "tok", "api_key": "k",
                     "content": "line1\nline2", "n": 3, "list": [{"password": "p"}], "empty_password": ""},
                    secret_names=["MY_CUSTOM"])
    assert out["env"] == {"MY_CUSTOM": "***", "OTHER": "ok"}
    assert out["confirmation_token"] == "***" and out["api_key"] == "***"
    assert out["content"] == "<11 characters not logged>"
    assert out["list"] == [{"password": "***"}] and out["n"] == 3
    assert mask_args("x" * 1000).endswith("...") and len(mask_args("x" * 1000)) < 400


# ------------------------------------------------------------------ audit log
def test_audit_records(service, tmp_path):
    async def go(c):
        await call(c, "deploy_server", **deploy_args())
        await call(c, "list_servers")
        await call(c, "get_server_status", server_id="nope")
        _, first = await call(c, "delete_server", server_id="one")
        await call(c, "delete_server", server_id="one", confirmation_token="bogus")
        await call(c, "delete_server", server_id="one", confirmation_token=first["confirmation_token"])

    app = make_app(service)
    run(app, go)
    lines = [json.loads(x) for x in (tmp_path / "data" / "mcp-audit.jsonl").read_text().splitlines()]
    assert [(x["tool"], x["status"]) for x in lines] == [
        ("deploy_server", "ok"), ("list_servers", "ok"), ("get_server_status", "error"),
        ("delete_server", "confirmation_required"), ("delete_server", "token_rejected"), ("delete_server", "ok")]
    deploy = lines[0]
    assert deploy["args"]["env"]["ADMIN_PASSWORD"] == "***" and deploy["args"]["name"] == "One"
    assert all({"time", "tool", "args", "status"} <= x.keys() for x in lines)
    assert lines[2]["detail"] == "Unknown server 'nope'"
    assert "bogus" not in json.dumps(lines)  # tokens are masked
    assert (tmp_path / "data" / "mcp-audit.jsonl").stat().st_mode & 0o777 == 0o600

    with TestClient(app) as c:
        entries = c.get("/api/mcp/audit?limit=3").json()["entries"]
    assert [e["status"] for e in entries] == ["ok", "token_rejected", "confirmation_required"]  # newest first


def test_audit_records_denied_connections(service, tmp_path):
    app = make_app(service, token="tok")
    post(app)
    post(app, {"Authorization": "Bearer wrong-secret-value"})
    lines = [json.loads(x) for x in (tmp_path / "data" / "mcp-audit.jsonl").read_text().splitlines()]
    assert lines and lines[0]["status"] == "denied" and "bearer" in lines[0]["detail"]
    assert "wrong-secret-value" not in json.dumps(lines)


def test_audit_log_rotation_and_bad_lines(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.record("t", {"a": 1}, "ok")
    with open(tmp_path / "a.jsonl", "a") as f:
        f.write("not json\n")
    log.record("t2", {}, "ok")
    assert [e["tool"] for e in log.recent()] == ["t2", "t"]
    assert log.recent(1)[0]["tool"] == "t2"


# ------------------------------------------------------------------ web routes
def test_info_route_never_contains_token(service):
    app = make_app(service, "full", token="top-secret-token")
    with TestClient(app, base_url="http://127.0.0.1:8080") as c:
        r = c.get("/api/mcp/info")
        d = r.json()
        assert d["enabled"] and d["mode"] == "full" and d["token_set"] is True
        assert d["endpoint_url"] == "http://127.0.0.1:8080/mcp"
        assert set(d["tools"]) == READ | STANDARD | DESTRUCTIVE
        assert "top-secret-token" not in r.text
    with TestClient(make_app(service, "standard"), base_url="http://127.0.0.1:8080") as c:
        assert c.get("/api/mcp/info").json()["token_set"] is False


def test_info_reports_forced_off(service):
    cfg = McpConfig.from_settings(Settings(host="0.0.0.0"))
    with TestClient(create_app(service, mcp_config=cfg)) as c:
        d = c.get("/api/mcp/info").json()
        assert d["enabled"] is False and d["configured_mode"] == "standard" and "TOKEN" in d["reason"]
        assert "TOKEN" in c.post("/mcp", json=RPC, headers=HDR).json()["error"]


# ------------------------------------------------------------------ end to end with the SDK client
def test_end_to_end_with_fake_backend(service, tmp_path):
    async def go(c):
        # guide resource + prompts
        res = await c.list_resources()
        assert [str(r.uri) for r in res.resources] == ["gamedock://guide"]
        guide = (await c.read_resource("gamedock://guide")).contents[0].text
        assert "two-step" in guide and "delete_server" in guide and "Current mode: full" in guide
        pr = {p.name for p in (await c.list_prompts()).prompts}
        assert pr == {"gamedock_overview", "deploy_game_server"}
        p = await c.get_prompt("deploy_game_server", {"game_id": "sample"})
        assert "get_catalog_entry" in p.messages[0].content.text

        err, cat = await call(c, "list_catalog")
        assert [g["id"] for g in cat["games"]] == ["sample"]
        err, entry = await call(c, "get_catalog_entry", game_id="sample")
        assert {f["name"] for f in entry["fields"]} >= {"ADMIN_PASSWORD"}
        err, msg = await call(c, "deploy_server", game_id="sample", name="One")  # missing required secret
        assert err and "Admin password" in msg

        err, d = await call(c, "deploy_server", **deploy_args(start=True))
        assert d["status"] == "running"
        err, clash = await call(c, "deploy_server", game_id="sample", name="Two", env=dict(ADMIN))
        assert err and "27015" in clash
        err, d = await call(c, "stop_server", server_id="one")
        assert d["status"] == "stopped"
        err, d = await call(c, "update_server_settings", server_id="one", name="Renamed", env={"MAX": "12"})
        assert not err, d
        assert d["name"] == "Renamed"
        err, d = await call(c, "start_server", server_id="one")
        assert d["status"] == "running"
        err, d = await call(c, "restart_server", server_id="one")
        assert d["status"] == "running"

        err, s = await call(c, "get_server_stats", server_id="one")
        assert s["running"] is True
        err, h = await call(c, "get_host_stats")
        assert h["cpu_count"] >= 1
        service.sample_stats()
        err, hist = await call(c, "get_stats_history", server_id="one", window_seconds=3600, max_points=5)
        assert not err and "points" in hist

        err, up = await call(c, "update_server_image", server_id="one")
        assert not err and "changed" in up

        (tmp_path / "data" / "one" / "conf").mkdir(parents=True, exist_ok=True)
        err, w = await call(c, "write_config_file", server_id="one", path="conf/hot.txt", content="hello\n")
        assert not err and w["size"] == 6
        err, r = await call(c, "read_config_file", server_id="one", path="conf/hot.txt")
        assert r["content"] == "hello\n"
        err, files = await call(c, "list_config_files", server_id="one")
        assert {f["path"] for f in files["files"]} == {"conf/settings.ini", "conf/hot.txt", "conf/data.json"}
        err, msg = await call(c, "read_config_file", server_id="one", path="../../etc/passwd")
        assert err

        err, b = await call(c, "create_backup", server_id="one")
        assert not err and b["backup"]["name"].endswith(".tar.gz")
        err, bl = await call(c, "list_backups", server_id="one")
        assert len(bl["backups"]) >= 1

        # destructive flow
        err, msg = await call(c, "delete_server", server_id="one")
        assert err and "Stop it first" in msg  # running servers cannot be deleted; no token is issued
        await call(c, "stop_server", server_id="one")
        err, first = await call(c, "delete_server", server_id="one", delete_data=True)
        assert first["status"] == "confirmation_required"
        err, done = await call(c, "delete_server", server_id="one", delete_data=True,
                               confirmation_token=first["confirmation_token"])
        assert done["status"] == "done"
        err, sv = await call(c, "list_servers")
        assert sv["servers"] == []

    run(make_app(service), go)


def test_web_and_mcp_see_the_same_servers(service):
    app = make_app(service)

    async def go(c):
        await call(c, "deploy_server", **deploy_args())
        return await call(c, "list_servers")

    _, via_mcp = run(app, go)
    with TestClient(app) as c:
        via_web = c.get("/api/servers").json()
    assert via_mcp["servers"] == via_web


def test_rejected_calls_are_audited(service, tmp_path):
    async def go(c):
        await c.call_tool("get_server_status", {})  # missing argument
        await c.call_tool("deploy_server", {"game_id": "sample", "name": "x", "env": {"ADMIN_PASSWORD": 12345678}})
        await c.call_tool("no_such_tool", {})

    run(make_app(service), go)
    lines = [json.loads(x) for x in (tmp_path / "data" / "mcp-audit.jsonl").read_text().splitlines()]
    assert [x["status"] for x in lines] == ["invalid_arguments", "invalid_arguments", "unknown_tool"]
    assert "server_id" in lines[0]["detail"]
    assert "12345678" not in json.dumps(lines[1])  # the rejected secret is masked, and not echoed in detail
    assert all(x.get("client") for x in lines)  # the caller's address is recorded
