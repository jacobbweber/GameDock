"""0.6.0: verify_game_setup (the "Check setup" checklist): read-only, concrete fixes, never touches the server."""
import json
import textwrap

import pytest
from fastapi.testclient import TestClient

from gamedock import checks as ck
from gamedock import steam
from gamedock.errors import NotFoundError
from gamedock.models import PortMapping
from gamedock.service import GameDockService
from gamedock.web.app import create_app
from tests.mcp_helpers import call, make_app, run

STEAMY = textwrap.dedent("""\
    id: steamy
    name: Steamy Game
    image: example/steamy:1
    ports:
      - { protocol: udp, host: 28015, container: 28015 }
    volumes:
      - { host: ./game, container: /game }
    fields:
      - { name: SERVER_NAME, label: Server name, default: "Steamy", required: true }
      - { name: ADMIN_PASSWORD, label: Admin password, secret: true, required: true }
    config_files:
      - { path: game/Config/Server.ini, label: Server settings, format: ini }
    ready_log_pattern: "Server is ready"
    steam_app_id: 555
    """)
ENV = {"SERVER_NAME": "Mine", "ADMIN_PASSWORD": "zz-top-secret"}


@pytest.fixture
def svc(backend, catalog_dir, tmp_path, clock):
    (catalog_dir / "steamy.yaml").write_text(STEAMY)
    return GameDockService(backend, catalog_dir, tmp_path / "data", clock=clock)


def by_id(r):
    return {c["id"]: c for c in r["checks"]}


def manifest(svc, app="555", build="1234", where="game/steamapps"):
    d = svc.data_dir / "steamy" / where
    d.mkdir(parents=True, exist_ok=True)
    (d / f"appmanifest_{app}.acf").write_text(steam.render_manifest(app, build))


def config(svc, text="[Server]\nName=Mine\nMaxPlayers=8\n"):
    p = svc.data_dir / "steamy" / "game" / "Config" / "Server.ini"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def healthy(svc, backend):
    svc.deploy("steamy", "Steamy", env=ENV, start=True)
    backend.set_logs("gamedock-steamy", "starting\nServer is ready\n")
    manifest(svc)
    config(svc)


def test_a_healthy_server_passes_every_check(svc, backend):
    healthy(svc, backend)
    r = svc.verify_game_setup("steamy")
    c = by_id(r)
    assert r["overall"] == "pass" and r["read_only"] is True and r["server_id"] == "steamy"
    assert c["container"]["status"] == "pass" and c["ready"]["status"] == "pass"
    assert c["version"]["status"] == "pass" and "build 1234" in c["version"]["detail"]
    assert c["config:game/Config/Server.ini"]["status"] == "pass"
    assert c["port:udp:28015"]["status"] == "pass" and c["volume:/game"]["status"] == "pass"
    assert c["settings"]["status"] == "pass" and c["catalog"]["status"] == "pass"
    assert "zz-top-secret" not in json.dumps(r) and r["summary"]


def test_wrong_steam_app_id_gets_the_exact_fix(svc, backend):
    svc.deploy("steamy", "Steamy", env=ENV, start=True)
    backend.set_logs("gamedock-steamy", "Server is ready\n")
    manifest(svc, app="2394010")
    c = by_id(svc.verify_game_setup("steamy"))["version"]
    assert c["status"] == "fail" and "appmanifest_2394010.acf" in c["detail"]
    assert "change steam_app_id to 2394010" in c["fix"]
    manifest(svc, app="777", where="game/other/steamapps")
    c = by_id(svc.verify_game_setup("steamy"))["version"]
    assert c["status"] == "fail" and "Several Steam records exist" in c["fix"]


def test_no_manifest_while_ready_says_where_to_mount(svc, backend):
    svc.deploy("steamy", "Steamy", env=ENV, start=True)
    backend.set_logs("gamedock-steamy", "Server is ready\n")
    c = by_id(svc.verify_game_setup("steamy"))["version"]
    assert c["status"] == "fail" and "mount the game's install folder" in c["fix"]


def test_no_manifest_before_ready_is_only_a_warning_or_skip(svc, backend):
    svc.deploy("steamy", "Steamy", env=ENV, start=True)
    backend.set_logs("gamedock-steamy", "downloading\n")
    assert by_id(svc.verify_game_setup("steamy"))["version"]["status"] == "warn"
    svc.stop("steamy")
    assert by_id(svc.verify_game_setup("steamy"))["version"]["status"] == "skip"


def test_missing_config_file_suggests_the_real_path(svc, backend):
    healthy(svc, backend)
    (svc.data_dir / "steamy" / "game" / "Config" / "Server.ini").unlink()
    other = svc.data_dir / "steamy" / "game" / "Saved" / "Server.ini"
    other.parent.mkdir(parents=True)
    other.write_text("[A]\nb=1\n")
    c = by_id(svc.verify_game_setup("steamy"))["config:game/Config/Server.ini"]
    assert c["status"] == "fail" and "game/Saved/Server.ini" in c["fix"]


def test_missing_config_file_without_lookalike_is_only_a_warning_early(svc, backend):
    healthy(svc, backend)
    (svc.data_dir / "steamy" / "game" / "Config" / "Server.ini").unlink()
    c = by_id(svc.verify_game_setup("steamy"))["config:game/Config/Server.ini"]
    assert c["status"] == "warn" and "first start" in c["fix"]


def test_empty_or_odd_config_file_is_reported(svc, backend):
    healthy(svc, backend)
    config(svc, "")
    c = by_id(svc.verify_game_setup("steamy"))["config:game/Config/Server.ini"]
    assert c["status"] == "warn" and "empty" in c["detail"]


def test_not_running_server_skips_runtime_checks(svc, backend):
    svc.deploy("steamy", "Steamy", env=ENV)
    c = by_id(svc.verify_game_setup("steamy"))
    assert c["ready"]["status"] == "skip" and c["port:udp:28015"]["status"] == "skip"
    assert c["container"]["status"] in ("warn", "pass")


def test_crashed_container_reports_exit_code_and_oom(svc, backend):
    svc.deploy("steamy", "Steamy", env=ENV, start=True)
    svc.stop("steamy")
    backend.exit_info["gamedock-steamy"] = (137, True)
    c = by_id(svc.verify_game_setup("steamy"))["container"]
    assert c["status"] == "fail" and "137" in c["detail"] and "ran out of memory" in c["detail"] and "more memory" in c["fix"]


def test_restarting_container_is_flagged(svc, backend):
    healthy(svc, backend)
    backend.restart_counts["gamedock-steamy"] = 6
    c = by_id(svc.verify_game_setup("steamy"))["container"]
    assert c["status"] in ("warn", "fail") and "6" in c["detail"]


def test_unpublished_port_is_a_problem(svc, backend):
    healthy(svc, backend)
    backend.unpublished_ports["gamedock-steamy"] = {"28015/udp"}
    c = by_id(svc.verify_game_setup("steamy"))["port:udp:28015"]
    assert c["status"] == "fail" and "not published" in c["detail"]


def test_port_bound_by_another_program_blocks_a_stopped_server(backend, catalog_dir, tmp_path):
    (catalog_dir / "steamy.yaml").write_text(STEAMY)
    probing = {"busy": False}
    s = GameDockService(backend, catalog_dir, tmp_path / "d", port_probe=lambda p, h: not probing["busy"])
    s.deploy("steamy", "Steamy", env=ENV)
    probing["busy"] = True
    c = by_id(s.verify_game_setup("steamy"))["port:udp:28015"]
    assert c["status"] == "fail" and "ss -lntup" in c["fix"]


def test_required_setting_missing_is_a_problem(svc, backend):
    healthy(svc, backend)
    inst = svc.store.get("steamy")
    inst.env["SERVER_NAME"] = ""
    svc.store.put(inst)
    c = by_id(svc.verify_game_setup("steamy"))["settings"]
    assert c["status"] == "fail" and "SERVER_NAME" in c["detail"] and "ADMIN_PASSWORD" not in c["detail"]


def test_catalog_drift_is_a_warning_with_the_plain_fix(svc, backend):
    healthy(svc, backend)
    (svc.catalog_dir / "steamy.yaml").write_text(STEAMY.replace("steam_app_id: 555", "steam_app_id: 556"))
    c = by_id(svc.verify_game_setup("steamy"))["catalog"]
    assert c["status"] == "warn" and "steam_app_id" in c["detail"] and "delete this test server" in c["fix"]


def test_verify_changes_nothing(svc, backend):
    healthy(svc, backend)
    before_files = sorted(str(p) for p in svc.data_dir.rglob("*"))
    before_calls = [c for c in backend.calls if c[0] in ("create", "start", "stop", "remove", "pull", "exec")]
    svc.verify_game_setup("steamy")
    svc.verify_game_setup("steamy")
    assert sorted(str(p) for p in svc.data_dir.rglob("*")) == before_files
    assert [c for c in backend.calls if c[0] in ("create", "start", "stop", "remove", "pull", "exec")] == before_calls


def test_unknown_server(svc):
    with pytest.raises(NotFoundError):
        svc.verify_game_setup("nope")


def test_summary_counts_and_suggestions(svc, backend):
    svc.deploy("steamy", "Steamy", env=ENV, start=True)
    backend.set_logs("gamedock-steamy", "Server is ready\n")
    r = svc.verify_game_setup("steamy")
    assert r["overall"] == "fail" and r["counts"]["fail"] >= 1 and r["suggestions"]
    assert all({"id", "title", "status", "detail"} <= set(c) for c in r["checks"])


def test_mcp_tool_and_web_endpoint(svc, backend):
    healthy(svc, backend)
    for mode in ("read_only", "standard", "full"):
        err, r = run(make_app(svc, mode), lambda c: call(c, "verify_game_setup", server_id="steamy"))
        assert not err and r["overall"] == "pass"
    err, msg = run(make_app(svc, "read_only"), lambda c: call(c, "verify_game_setup", server_id="nope"))
    assert err
    with TestClient(create_app(svc)) as c:
        w = c.get("/api/servers/steamy/check-setup")
        assert w.status_code == 200 and w.json()["overall"] == "pass"
        assert c.get("/api/servers/nope/check-setup").status_code == 404


def test_audit_of_the_tool_has_no_secret(svc, backend):
    healthy(svc, backend)
    run(make_app(svc, "standard"), lambda c: call(c, "verify_game_setup", server_id="steamy"))
    text = (svc.data_dir / "mcp-audit.jsonl").read_text()
    assert "verify_game_setup" in text and "zz-top-secret" not in text


# ------------------------------------------------------------------ pure helpers
def test_redact_text_masks_known_secrets_and_shapes():
    s = ("login password=hunter2 ok\nAuthorization: Bearer abc.def.ghi\nurl https://user:pw123@host/x\n"
         "token: sk-12345678\nthe value zz-top-secret leaked\nnothing here")
    out = ck.redact_text(s, ("zz-top-secret",))
    for leak in ("hunter2", "abc.def.ghi", "pw123", "sk-12345678", "zz-top-secret"):
        assert leak not in out
    assert "nothing here" in out and "login" in out and "host/x" in out


def test_redact_text_ignores_short_or_empty_secrets():
    assert ck.redact_text("a b a", ("", "a")) == "a b a"


@pytest.mark.parametrize("fmt,text,ok", [
    ("json", '{"a": 1}', True), ("json", "{oops", False), ("yaml", "a: 1\nb: [1, 2]\n", True), ("yaml", "a: [1\n", False),
    ("ini", "[s]\na=1\n", True), ("text", "hello", True), ("text", "", False)])
def test_check_config_text(fmt, text, ok):
    status, detail, fix = ck.check_config_text(fmt, text, None, 0)
    assert (status == ck.PASS) == ok
    assert detail and (ok or fix)


def test_find_by_name_is_bounded(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "x.ini").write_text("1")
    assert ck.find_by_name(tmp_path, "x.ini") == ["a/b/x.ini"]
    assert ck.find_by_name(tmp_path, "nope.ini") == []


def test_summarize():
    s = ck.summarize([ck.Check("a", "A", ck.PASS), ck.Check("b", "B", ck.WARN), ck.Check("c", "C", ck.SKIP)])
    assert s["overall"] == "warn" and s["counts"] == {"pass": 1, "warn": 1, "fail": 0, "skip": 1}


def test_list_manifests_finds_all_and_reads_names(tmp_path):
    for app, name in (("10", "One"), ("20", "Two")):
        d = tmp_path / app / "steamapps"
        d.mkdir(parents=True)
        (d / f"appmanifest_{app}.acf").write_text(steam.render_manifest(app, "9"))
    got = steam.list_manifests([tmp_path])
    assert sorted(m["app_id"] for m in got) == ["10", "20"] and all(m["build"] == "9" for m in got)
    assert steam.list_manifests([tmp_path / "missing"]) == []
