"""0.6.2: files a game container wrote as another user (usually root) no longer block backups, config edits, restores,
wipes or deletes. GameDock repairs the owners with a short-lived helper container that mounts only that server's data
folder, and catalog entries can make the game run as GameDock's user in the first place (user_env / run_as_user)."""
import os
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from gamedock import authoring as au
from gamedock import problems as pb
from gamedock.backends import DockerCliBackend
from gamedock.catalog import CatalogError, load_catalog, parse_definition
from gamedock.errors import AccessError, BackendError, GameDockError
from gamedock.models import ContainerSpec
from gamedock.web.app import create_app
from starlette.testclient import TestClient

from .conftest import ADMIN
from .mcp_helpers import call, make_app, run

ROOT = Path(__file__).resolve().parents[1]
needs_non_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read everything")


def data(tmp_path):
    return tmp_path / "data" / "one"


def root_owned(tmp_path):
    """Pretend the game container wrote files as root with mode 600/700: unreadable for GameDock's user."""
    d = data(tmp_path) / "config"
    d.mkdir(parents=True, exist_ok=True)
    (d / "world.db").write_text("precious world")
    (d / "world.db").chmod(0)
    sub = d / "worlds_local"
    sub.mkdir()
    (sub / "w.fwl").write_text("x")
    sub.chmod(0)
    return d


def unlock(tmp_path):
    for root, dirs, files in os.walk(tmp_path, onerror=lambda e: None):
        for n in dirs + files:
            try:
                os.chmod(os.path.join(root, n), 0o755)
            except OSError:
                pass


@pytest.fixture
def srv(service, deployed, tmp_path):
    yield service
    unlock(tmp_path)


# ------------------------------------------------------------------ automatic repair, then retry once
@needs_non_root
def test_delete_with_data_repairs_owners_and_backup_is_complete(srv, tmp_path):
    root_owned(tmp_path)
    r = srv.delete("one", confirm=True, delete_data=True, confirm_delete_data=True)
    assert not data(tmp_path).exists() and srv.store.get("one") is None
    assert "gave them back to its own user automatically" in r.note
    calls = srv.backend.ownership_calls
    assert calls and all(c == (str(data(tmp_path).resolve()), os.getuid(), os.getgid()) for c in calls)
    with tarfile.open(r.backup.path) as tar:          # the backup is complete: the root-owned files are in it
        names = tar.getnames()
        assert "config/world.db" in names and "config/worlds_local/w.fwl" in names
        assert tar.extractfile("config/world.db").read() == b"precious world"


@needs_non_root
def test_delete_keeps_everything_when_the_helper_fails(srv, tmp_path):
    root_owned(tmp_path)
    srv.backend.ownership_fail = "helper image could not be downloaded"
    with pytest.raises(AccessError) as e:
        srv.delete("one", confirm=True, delete_data=True, confirm_delete_data=True)
    msg = str(e.value)
    assert "tried to give the files back" in msg and "helper image could not be downloaded" in msg
    assert "docker run --rm" in msg   # the manual fallback that works without sudo
    assert srv.store.get("one") is not None and data(tmp_path).exists()   # nothing deleted, entry kept for a retry
    assert not list((tmp_path / "data" / "backups" / "_deleted").glob("*.tar.gz"))


@needs_non_root
def test_wipe_restore_and_backup_repair_owners(srv, tmp_path):
    root_owned(tmp_path)
    b = srv.create_backup("one")
    assert "automatically" in b.note
    with tarfile.open(b.backup.path) as tar:
        assert "config/world.db" in tar.getnames()
    root_owned_again = data(tmp_path) / "config" / "new.dat"
    root_owned_again.write_text("y")
    root_owned_again.chmod(0)
    r = srv.restore_backup("one", b.backup.name, confirm=True)
    assert (data(tmp_path) / "config" / "world.db").read_text() == "precious world"
    (data(tmp_path) / "config" / "world.db").chmod(0)
    w = srv.wipe_data("one", confirm=True)
    assert "automatically" in w.note
    assert [p.name for p in data(tmp_path).iterdir()] == ["saves"]   # recreated empty folder only
    assert r.safety_backup


@needs_non_root
def test_config_read_and_write_repair_owners(srv, tmp_path):
    conf = data(tmp_path) / "conf"
    conf.mkdir()
    f = conf / "hot.txt"
    f.write_text("a\n")
    f.chmod(0)
    assert srv.read_config_file("one", "conf/hot.txt").content == "a\n"
    f.chmod(0o444)
    conf.chmod(0o555)
    srv.write_config_file("one", "conf/hot.txt", "b\n")
    assert f.read_text() == "b\n"
    assert len(srv.backend.ownership_calls) == 2


def test_proactive_repair_before_delete(srv, tmp_path, monkeypatch):
    from gamedock import service as svc_mod
    (data(tmp_path) / "saves" / "x").write_text("x")
    seen = {"n": 0}

    def fake_owners(root, cap=20000):
        seen["n"] += 1
        return ({0}, str(Path(root) / "saves" / "x")) if not srv.backend.ownership_calls else (set(), None)

    monkeypatch.setattr(svc_mod, "foreign_owners", fake_owners)
    srv.delete("one", confirm=True, delete_data=True, confirm_delete_data=True)
    assert len(srv.backend.ownership_calls) == 1 and seen["n"] >= 1   # repaired once, before the backup


def test_no_repair_when_nothing_is_wrong(srv, tmp_path):
    srv.create_backup("one")
    srv.delete("one", confirm=True, delete_data=True, confirm_delete_data=True)
    assert srv.backend.ownership_calls == []


# ------------------------------------------------------------------ strict safety of the helper's target
@needs_non_root
def test_symlinked_data_folder_is_refused(service, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "saves").mkdir()
    service.deploy("sample", "One", env=ADMIN)
    d = data(tmp_path)
    import shutil
    shutil.rmtree(d)
    d.symlink_to(outside, target_is_directory=True)
    inst = service._instance("one")
    with pytest.raises(GameDockError, match="symbolic link|not inside"):
        service._ownership_target(inst)
    with pytest.raises(GameDockError):
        service.fix_data_ownership("one")
    assert service.backend.ownership_calls == []


def test_bad_server_ids_are_refused(service, deployed):
    from gamedock.models import Instance
    for bad in ("..", "../x", "backups", "a/b", ""):
        inst = Instance(bad, "x", "sample", {}, ())
        with pytest.raises(GameDockError):
            service._ownership_target(inst)


def test_target_is_exactly_the_server_folder(service, deployed, tmp_path):
    assert service._ownership_target(service._instance("one")) == data(tmp_path).resolve()


# ------------------------------------------------------------------ the Docker helper container
def test_chown_args_mount_only_one_folder_without_network():
    a = DockerCliBackend.build_chown_args("alpine:3", "/srv/gd/data/valheim", 1000, 1000)
    assert a[:2] == ["run", "--rm"] and a[a.index("--network") + 1] == "none"
    assert a.count("--mount") == 1 and a[a.index("--mount") + 1] == "type=bind,source=/srv/gd/data/valheim,target=/d"
    assert "-v" not in a and "--privileged" not in a and "--read-only" in a
    assert a[a.index("--cap-drop") + 1] == "ALL"
    assert a[a.index("--") + 1:] == ["alpine:3", "-R", "-h", "1000:1000", "/d"]
    assert a[a.index("--entrypoint") + 1] == "chown"


@pytest.mark.parametrize("path,uid,gid", [("relative/dir", 1, 1), ("/x\n", 1, 1), ("/x", -1, 1), ("/x", True, 1)])
def test_chown_args_refuse_bad_input(path, uid, gid):
    with pytest.raises(BackendError):
        DockerCliBackend.build_chown_args("alpine:3", path, uid, gid)


class Runner:
    def __init__(self, present=(), pull_ok=True):
        self.present, self.pull_ok, self.calls = set(present), pull_ok, []

    def __call__(self, argv, **kw):
        self.calls.append(argv[1:])
        args = argv[1:]
        if args[:2] == ["image", "inspect"]:
            img = args[-1]
            return subprocess.CompletedProcess(argv, 0 if img in self.present else 1,
                                               "sha256:abc\n" if img in self.present else "", "")
        if args[0] == "pull":
            if self.pull_ok:
                self.present.add(args[-1])
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", "Error response from daemon: dial tcp: lookup registry")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_helper_uses_the_configured_image_when_present():
    r = Runner(present={"alpine:3"})
    note = DockerCliBackend(runner=r).repair_ownership("/d/x", 1000, 1000)
    assert "already on this machine" in note and not any(c[0] == "pull" for c in r.calls)
    assert r.calls[-1][0] == "run" and "alpine:3" in r.calls[-1]


def test_helper_reuses_a_small_image_already_present():
    r = Runner(present={"busybox:latest"})
    note = DockerCliBackend(runner=r).repair_ownership("/d/x", 1000, 1000)
    assert "busybox:latest" in note and not any(c[0] == "pull" for c in r.calls)
    assert "busybox:latest" in r.calls[-1]


def test_helper_pulls_only_when_needed_with_a_clear_message():
    r = Runner()
    note = DockerCliBackend(runner=r, helper_image="busybox:1.36").repair_ownership("/d/x", 1000, 1000)
    assert "downloaded the small helper image busybox:1.36" in note
    assert ["pull", "--", "busybox:1.36"] in r.calls
    r2 = Runner(pull_ok=False)
    with pytest.raises(BackendError, match="GAMEDOCK_HELPER_IMAGE"):
        DockerCliBackend(runner=r2).repair_ownership("/d/x", 1000, 1000)
    assert not any(c[0] == "run" for c in r2.calls)


def test_helper_image_setting_is_validated(monkeypatch):
    from gamedock.config import Settings
    monkeypatch.setenv("GAMEDOCK_HELPER_IMAGE", "busybox:1.36")
    assert Settings.from_env().helper_image == "busybox:1.36"
    monkeypatch.setenv("GAMEDOCK_HELPER_IMAGE", "bad image; rm")
    with pytest.raises(ValueError):
        Settings.from_env()
    with pytest.raises(BackendError):
        DockerCliBackend(helper_image="--privileged")


# ------------------------------------------------------------------ Fix ownership (web, MCP, Check setup)
def test_check_setup_flags_foreign_files_with_a_fix_button(service, deployed, monkeypatch):
    from gamedock import ops_service
    c = {x["id"]: x for x in service.verify_game_setup("one")["checks"]}
    assert c["owner"]["status"] == "pass" and "action" not in c["owner"]
    monkeypatch.setattr(ops_service, "foreign_owners", lambda root, cap=20000: ({0}, str(root / "saves" / "w.db")))
    c = {x["id"]: x for x in service.verify_game_setup("one")["checks"]}
    o = c["owner"]
    assert o["status"] == "warn" and o["action"] == "fix_ownership" and "root (0)" in o["detail"]
    assert "Fix ownership" in o["fix"] and "fix_data_ownership" in o["fix"] and "user_env" in o["fix"]


def test_fix_ownership_endpoint_is_non_destructive(service, deployed, tmp_path, monkeypatch):
    from gamedock import service as svc_mod
    (data(tmp_path) / "saves" / "keep.dat").write_text("k")
    client = TestClient(create_app(service))
    r = client.post("/api/servers/one/fix-ownership").json()
    assert r["changed"] is False and "Nothing was changed" in r["message"]
    state = {"fixed": False}

    def owners(root, cap=20000):
        return (set(), None) if state["fixed"] else ({0}, str(root / "saves" / "keep.dat"))

    def repair(host_dir, uid, gid):
        state["fixed"] = True
        service.backend.ownership_calls.append((host_dir, uid, gid))
        return "used the helper image alpine:3 (already on this machine)"

    monkeypatch.setattr(svc_mod, "foreign_owners", owners)
    monkeypatch.setattr(service.backend, "repair_ownership", repair)
    r = client.post("/api/servers/one/fix-ownership").json()
    assert r["changed"] and r["before"] == [0] and r["after"] == [] and "Nothing was deleted" in r["message"]
    assert (data(tmp_path) / "saves" / "keep.dat").read_text() == "k"
    assert any("user_env" in w for w in r["warnings"])      # the sample entry does not run as GameDock's user


def test_fix_ownership_failure_is_plain(service, deployed, monkeypatch):
    from gamedock import service as svc_mod
    monkeypatch.setattr(svc_mod, "foreign_owners", lambda root, cap=20000: ({0}, str(root)))
    service.backend.ownership_fail = "Docker is not running"
    r = TestClient(create_app(service)).post("/api/servers/one/fix-ownership")
    assert r.status_code >= 400 and "Docker is not running" in r.json()["error"]


def test_mcp_fix_data_ownership_only_in_standard_and_full_and_audited(service, deployed, tmp_path):
    async def names(c):
        return {t.name for t in (await c.list_tools()).tools}

    assert "fix_data_ownership" not in run(make_app(service, "read_only"), names)
    assert "fix_data_ownership" in run(make_app(service, "standard"), names)

    async def go(c):
        return await call(c, "fix_data_ownership", server_id="one")

    err, r = run(make_app(service, "full"), go)
    assert not err and r["changed"] is False
    audit = (tmp_path / "data" / "mcp-audit.jsonl").read_text()
    assert "fix_data_ownership" in audit and "nothing to change" in audit


def test_mcp_tool_annotations_say_not_destructive(service):
    from gamedock.mcp_api.confirm import ConfirmationStore
    from gamedock.mcp_api.tools import STANDARD, make_tools
    d = next(t for t in make_tools(service, ConfirmationStore()) if t.name == "fix_data_ownership")
    assert d.tier == STANDARD and d.annotations.destructive_hint is False


# ------------------------------------------------------------------ running as GameDock's user (catalog)
USER_ENTRY = {"id": "g", "name": "G", "image": "x/y:1", "volumes": [{"host": "./d", "container": "/d"}],
              "ports": [{"protocol": "udp", "host": 29999, "container": 29999}],
              "user_env": {"PUID": "uid", "PGID": "gid"}}


def test_user_env_is_filled_with_gamedock_ids(service, catalog_dir, tmp_path):
    (catalog_dir / "g.yaml").write_text(yaml.safe_dump(USER_ENTRY))
    info = service.deploy("g", "G")
    spec = service._spec(service._instance(info.id))
    assert spec.env["PUID"] == str(os.getuid()) and spec.env["PGID"] == str(os.getgid()) and spec.user == ""
    args = DockerCliBackend.build_create_args(spec)
    assert "--user" not in args and "PUID" in args


def test_run_as_user_passes_docker_user(service, catalog_dir):
    (catalog_dir / "g.yaml").write_text(yaml.safe_dump({**USER_ENTRY, "user_env": {}, "run_as_user": True}))
    info = service.deploy("g", "G")
    spec = service._spec(service._instance(info.id))
    assert spec.user == f"{os.getuid()}:{os.getgid()}"
    args = DockerCliBackend.build_create_args(spec)
    assert args[args.index("--user") + 1] == spec.user
    p = service.preview_deploy("g", "G2")
    assert f"--user {spec.user}" in p["container_command"]


def test_bad_docker_user_is_refused():
    with pytest.raises(BackendError):
        DockerCliBackend.build_create_args(ContainerSpec("gamedock-x", "img:1", (), (), {}, user="root"))


@pytest.mark.parametrize("change,msg", [
    ({"user_env": {"PUID": "1000"}}, "uid or gid"),
    ({"user_env": ["PUID"]}, "mapping"),
    ({"user_env": {"PUID": "uid"}, "env": {"PUID": "1000"}}, "also in"),
    ({"user_env": {"PATH": "uid"}}, "bad name"),
    ({"run_as_user": "yes"}, "true or false"),
    ({"user_env": {f"V{i}": "uid" for i in range(5)}}, "at most"),
])
def test_user_keys_are_validated(change, msg):
    with pytest.raises(CatalogError, match=msg):
        parse_definition({**USER_ENTRY, **change}, "t")


def test_user_keys_round_trip():
    from gamedock.catalog import entry_to_dict
    e = parse_definition({**USER_ENTRY, "run_as_user": True}, "t")
    d = entry_to_dict(e)
    assert d["user_env"] == {"PUID": "uid", "PGID": "gid"} and d["run_as_user"] is True
    assert parse_definition(d, "again") == e


def test_bundled_entries_run_as_the_gamedock_user():
    cat = load_catalog(ROOT / "catalog")
    assert cat["valheim"].user_env == {"PUID": "uid", "PGID": "gid"}
    assert cat["palworld"].user_env == {"PUID": "uid", "PGID": "gid"}
    assert cat["enshrouded"].user_env == {"PUID": "uid", "PGID": "gid"}
    assert cat["satisfactory"].user_env == {"PUID": "uid", "PGID": "gid"}
    assert cat["minecraft-java"].user_env == {"UID": "uid", "GID": "gid"}
    for e in cat.values():
        assert not au.user_id_env_names(e.env), e.id   # never a fixed number any more


def test_lint_warns_about_fixed_or_unmapped_user_ids():
    e = parse_definition({**USER_ENTRY, "user_env": {}, "env": {"PUID": "1000"}}, "t")
    assert any("user_env" in f.text and "PUID" in f.text for f in au.analyze_entry(e))
    plain = parse_definition({**USER_ENTRY, "user_env": {}}, "t")
    rec = {"ok": True, "facts": {"env_names": ["PUID", "PGID", "SERVER_NAME"], "exposed_ports": [], "volumes": [],
                                 "healthcheck": True}}
    flags = [f.text for f in au.analyze_image_facts(plain, rec)]
    assert any("user id variables (PUID, PGID)" in t for t in flags)
    mapped = parse_definition(USER_ENTRY, "t")
    assert not any("user id variables" in f.text for f in au.analyze_image_facts(mapped, rec))
    assert not any("PUID" in t for t in [f.text for f in au.analyze_image_facts(mapped, rec)])


def test_docs_describe_the_new_keys():
    guide = (ROOT / "docs" / "ADDING_A_GAME.md").read_text()
    assert "`user_env`" in guide and "`run_as_user`" in guide
    assert "user_env" in (ROOT / "docs" / "PROMPT_ADD_A_GAME.md").read_text()
    assert "user_env" in (ROOT / "catalog" / "_template.yaml").read_text()


# ------------------------------------------------------------------ bundled small items
def test_gslt_expired_is_a_builtin_problem():
    p = pb.scan([("docker logs", "Failed to connect to Steam reason: k_EResultGSLTExpired\nCustomAuthenticationFailed")])
    assert p.id == "steam-token-expired" and "expired" in p.title
    assert "managegameservers" in p.advice and "phone number" in p.advice


def test_aska_example_names_the_expired_token_first():
    e = parse_definition(yaml.safe_load((ROOT / "docs" / "catalog-entries" / "aska.yaml").read_text()), "aska")
    p = pb.scan([("docker logs", "k_EResultGSLTExpired")], e.fail_log_patterns)
    assert p.source == pb.SOURCE_CATALOG and "expired" in p.title
    p2 = pb.scan([("docker logs", "CustomAuthenticationFailed k_EResultAccountNotFound")], e.fail_log_patterns)
    assert "rejected" in p2.title


def test_palworld_notes_explain_the_password_popup():
    notes = load_catalog(ROOT / "catalog")["palworld"].notes
    assert "Input password" in notes and "single space" in notes
