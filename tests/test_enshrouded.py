"""The Enshrouded catalog entry, checked against what a real run showed (mornedhels/enshrouded-server:stable-wine):
the password rule (an empty role password must reach the container as a present, empty variable), the persistent
game folder, the version file, the settings file with nested objects, and the per-game stop timeout."""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gamedock import configforms as cf
from gamedock import readiness as rd
from gamedock import steam
from gamedock.backends import DockerCliBackend, FakeBackend
from gamedock.catalog import load_catalog, parse_definition
from gamedock.configfiles import MASK
from gamedock.errors import CatalogError
from gamedock.models import ConfigFile, ContainerSpec, PortMapping, VolumeMapping
from gamedock.service import GameDockService

CATALOG = Path(__file__).resolve().parent.parent / "catalog"
SETTINGS_PATH = "game/server/enshrouded_server.json"

# Shaped like the file the game wrote on the real host: tabs, no trailing commas, one user group.
REAL_JSON = """{
\t"name": "My Enshrouded Server",
\t"saveDirectory": "./savegame",
\t"logDirectory": "./logs",
\t"ip": "0.0.0.0",
\t"queryPort": 15637,
\t"slotCount": 16,
\t"tags": [],
\t"voiceChatMode": "Proximity",
\t"enableVoiceChat": false,
\t"enableTextChat": false,
\t"gameSettingsPreset": "Default",
\t"gameSettings": {
\t\t"playerHealthFactor": 1,
\t\t"playerManaFactor": 1,
\t\t"playerStaminaFactor": 1,
\t\t"enableDurability": true,
\t\t"enableStarvingDebuff": false,
\t\t"foodBuffDurationFactor": 1.5,
\t\t"miningDamageFactor": 1,
\t\t"tombstoneMode": "AddBackpackMaterials",
\t\t"weatherFrequency": "Normal"
\t},
\t"userGroups": [
\t\t{
\t\t\t"name": "Default",
\t\t\t"password": "Xk3mQ9ra",
\t\t\t"canKickBan": false,
\t\t\t"canAccessInventories": true,
\t\t\t"canEditWorld": true,
\t\t\t"canEditBase": true,
\t\t\t"canExtendBase": true,
\t\t\t"reservedSlots": 0
\t\t},
\t\t{
\t\t\t"name": "Admin",
\t\t\t"password": "",
\t\t\t"canKickBan": true,
\t\t\t"canAccessInventories": true,
\t\t\t"canEditWorld": true,
\t\t\t"canEditBase": true,
\t\t\t"canExtendBase": true,
\t\t\t"reservedSlots": 2
\t\t}
\t],
\t"bannedAccounts": []
}
"""
CF = ConfigFile(SETTINGS_PATH, format="json", sensitive_keys=("password",))


@pytest.fixture(scope="module")
def ens():
    return load_catalog(CATALOG)["enshrouded"]


# ------------------------------------------------------------------ the catalog entry
def test_password_is_sent_through_the_role_variable_even_when_empty(ens):
    names = {f.name for f in ens.fields}
    assert "SERVER_PASSWORD" not in names and "SERVER_ROLE_0_PASSWORD" in names
    f = next(f for f in ens.fields if f.name == "SERVER_ROLE_0_PASSWORD")
    assert f.secret and f.label == "Server password" and f.default == "" and not f.required
    assert ens.env["SERVER_ROLE_0_NAME"] == "Default"
    assert {k: v for k, v in ens.env.items() if k.startswith("SERVER_ROLE_0_") and k != "SERVER_ROLE_0_NAME"} == {
        "SERVER_ROLE_0_CAN_KICK_BAN": "false", "SERVER_ROLE_0_CAN_ACCESS_INVENTORIES": "true",
        "SERVER_ROLE_0_CAN_EDIT_WORLD": "true", "SERVER_ROLE_0_CAN_EDIT_BASE": "true",
        "SERVER_ROLE_0_CAN_EXTEND_BASE": "true", "SERVER_ROLE_0_RESERVED_SLOTS": "0"}
    assert ens.env["SERVER_QUERYPORT"] == "15637"
    assert "PUID" not in ens.env and ens.user_env == {"PUID": "uid", "PGID": "gid"}   # 0.6.2: GameDock's own ids
    assert all(isinstance(v, str) for v in ens.env.values())   # "false" must not turn into "False"


def test_game_folder_is_persistent_and_stop_timeout_is_90(ens):
    assert VolumeMapping("./game", "/opt/enshrouded") in ens.volumes
    assert VolumeMapping("./savegame", "/home/steam/enshrouded/savegame") in ens.volumes
    assert VolumeMapping("./logs", "/home/steam/enshrouded/logs") in ens.volumes
    assert ens.stop_timeout == 90 and ens.installed_build_file == "game/current_version"
    assert ens.steam_app_id == "2278520"


def test_settings_file_is_declared_stopped_only_with_masked_password(ens):
    (c,) = ens.config_files
    assert (c.path, c.format, c.sensitive_keys, c.hot_edit) == (SETTINGS_PATH, "json", ("password",), False)
    assert "written into this file again" in c.note and "Stop the server" in c.note
    assert "7 GB" in ens.notes and "Installing" in ens.notes


def test_ready_and_progress_patterns_match_the_real_log(ens):
    session = "[00:31:07] enshrouded-server -------------- Session ----------------\n[00:31:07] enshrouded-server Machines:"
    assert rd.pattern_seen(session, ens.ready_log_pattern)
    for early in ("enshrouded-updater Waiting for client config",
                  "enshrouded-updater Failed to install app 2278520 (Missing configuration)",
                  "enshrouded-server Config Parsed: /opt/enshrouded/server/enshrouded_server.json"):
        assert not rd.pattern_seen(early, ens.ready_log_pattern)
    line = " Update state (0x61) downloading, progress: 51.68 (2528448160 / 4891838742)"
    assert rd.last_progress(line, ens.progress_log_pattern) == 51.68
    # while downloading there is no Session block yet: the phase is installing, then ready
    assert rd.decide("", True, False, 51.68).phase == rd.PHASE_INSTALLING
    assert rd.decide("", True, True, None).ready


# ------------------------------------------------------------------ empty values reach docker as "NAME="
SPEC = ContainerSpec("gamedock-e", "img:1", (PortMapping("udp", 15637, 15637),),
                     (VolumeMapping("/d/game", "/opt/enshrouded"),),
                     {"SERVER_ROLE_0_PASSWORD": "", "SERVER_ROLE_0_NAME": "Default", "SECRET": "hunter2"})


def pairs(args):
    return [args[i + 1] for i, a in enumerate(args) if a == "-e"]


def test_empty_env_values_are_passed_as_present_but_empty_and_secrets_stay_off_the_command_line():
    args = DockerCliBackend.build_create_args(SPEC)
    assert pairs(args) == ["SERVER_ROLE_0_PASSWORD=", "SERVER_ROLE_0_NAME", "SECRET"]
    assert "hunter2" not in " ".join(args)


def test_environment_handed_to_docker_still_holds_the_values():
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 1 if argv[1] == "container" else 0, "", "No such container: x")

    DockerCliBackend(runner=runner).create(SPEC)
    kw = calls[-1][1]
    assert kw["env"]["SERVER_ROLE_0_NAME"] == "Default" and kw["env"]["SECRET"] == "hunter2"


def test_a_stored_server_with_an_empty_password_still_sends_the_variable(tmp_path):
    (tmp_path / "cat").mkdir()
    shutil.copy(CATALOG / "enshrouded.yaml", tmp_path / "cat" / "enshrouded.yaml")
    svc = GameDockService(FakeBackend(), tmp_path / "cat", tmp_path / "data")
    sid = svc.deploy("enshrouded", "Ens").id
    spec = svc._spec(svc.store.get(sid))
    assert spec.env["SERVER_ROLE_0_PASSWORD"] == "" and spec.env["SERVER_ROLE_0_NAME"] == "Default"
    assert "-e" in DockerCliBackend.build_create_args(spec) and \
        "SERVER_ROLE_0_PASSWORD=" in pairs(DockerCliBackend.build_create_args(spec))
    assert spec.stop_timeout == 90
    assert "--stop-timeout" in (a := DockerCliBackend.build_create_args(spec, spec.stop_timeout or 60))
    assert a[a.index("--stop-timeout") + 1] == "90"
    # host folders are created by GameDock (its own user), including the game folder
    svc._prepare_dirs(spec)
    assert (tmp_path / "data" / sid / "game").is_dir()


def test_per_game_stop_timeout_is_used_for_create_and_stop():
    spec = ContainerSpec("gamedock-e", "img:1", (), (), {}, {}, stop_timeout=90)
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw))
        out = "running|0|false|true\n" if argv[1:3] == ["container", "inspect"] else ""
        rc = 1 if (argv[1:3] == ["container", "inspect"] and argv[-1] == "gamedock-e" and len(seen) == 1) else 0
        return subprocess.CompletedProcess(argv, rc, out, "No such container: gamedock-e" if rc else "")

    b = DockerCliBackend(runner=runner, stop_timeout=60)
    b.create(spec)
    assert seen[1][0][seen[1][0].index("--stop-timeout") + 1] == "90"
    seen.clear()
    b.stop("gamedock-e", 90)
    assert seen[1][0] == ["docker", "stop", "-t", "90", "gamedock-e"] and seen[1][1]["timeout"] >= 120
    seen.clear()
    b.stop("gamedock-e")
    assert seen[1][0][:3] == ["docker", "stop", "-t"] and seen[1][0][3] == "60"


def test_service_stops_with_the_games_timeout(tmp_path):
    (tmp_path / "cat").mkdir()
    shutil.copy(CATALOG / "enshrouded.yaml", tmp_path / "cat" / "enshrouded.yaml")
    backend = FakeBackend()
    asked = []
    orig = backend.stop
    backend.stop = lambda name, timeout=None: (asked.append(timeout), orig(name, timeout))[1]
    svc = GameDockService(backend, tmp_path / "cat", tmp_path / "data")
    sid = svc.deploy("enshrouded", "Ens", start=True).id
    svc.stop(sid)
    assert asked == [90]


def test_catalog_validation_of_the_new_options():
    base = "id: g\nname: G\nimage: i:1\n"
    assert parse_definition({"id": "g", "name": "G", "image": "i:1", "stop_timeout": 45}).stop_timeout == 45
    for bad in ({"stop_timeout": 0.5}, {"stop_timeout": "ninety"}, {"stop_timeout": 601}, {"stop_timeout": -1},
                {"installed_build_file": "../x"}, {"installed_build_file": "/etc/x"}, {"installed_build_file": 5}):
        with pytest.raises(CatalogError):
            parse_definition({"id": "g", "name": "G", "image": "i:1", **bad})
    assert parse_definition({"id": "g", "name": "G", "image": "i:1"}).installed_build_file == ""


# ------------------------------------------------------------------ the version file
def test_version_file_reader(tmp_path):
    f = tmp_path / "current_version"
    f.write_text("\n  25247047\n")
    g = steam.read_version_file(f)
    assert (g.build, g.comparable, g.complete) == ("25247047", True, True)
    f.write_text("0.7.1.2\n")
    g = steam.read_version_file(f)
    assert (g.build, g.comparable) == ("0.7.1.2", False)
    for junk in ("", "   \n", "a" * 80, "<html>nope</html>", "x\x00y"):
        f.write_text(junk)
        assert steam.read_version_file(f) is None
    assert steam.read_version_file(tmp_path / "missing") is None
    (tmp_path / "link").symlink_to(f)
    f.write_text("123")
    assert steam.read_version_file(tmp_path / "link") is None      # never follow a symlink


def test_status_with_text_that_is_not_a_build_number():
    inst = steam.InstalledGame("0.7.1.2", None, comparable=False)
    st, text = steam.compute_status(True, inst, steam.LatestBuild("2278520", "public", True, "25300000"))
    assert st == steam.COULD_NOT_CHECK and "0.7.1.2" in text and "not a Steam build number" in text


class Mirror:
    def __init__(self, build="25300000"):
        self.build = build

    def __call__(self, url, timeout):
        return 200, json.dumps({"data": {"2278520": {"depots": {"branches": {"public": {
            "buildid": self.build, "timeupdated": "1792297341"}}}}}})


@pytest.fixture
def world(tmp_path):
    (tmp_path / "cat").mkdir()
    shutil.copy(CATALOG / "enshrouded.yaml", tmp_path / "cat" / "enshrouded.yaml")
    mirror = Mirror()
    info = steam.SteamInfo(tmp_path / "data" / "steam-latest.json", http_get=mirror, manual_gap=0)
    svc = GameDockService(FakeBackend(), tmp_path / "cat", tmp_path / "data", steam_info=info)
    sid = svc.deploy("enshrouded", "Ens").id
    svc._manifest_missing.clear()
    return svc, sid, tmp_path / "data" / sid, mirror


def test_installed_version_comes_from_the_version_file(world):
    svc, sid, data, mirror = world
    r = svc.check_game_update(sid)                       # nothing installed yet
    assert r["status"] == "could_not_check" and not r["installed"]["found"]
    assert "game/current_version" in r["summary"] and "Steam record" in r["summary"]
    (data / "game").mkdir(exist_ok=True)
    (data / "game" / "current_version").write_text("25247047\n")
    svc._manifest_missing.clear()
    r = svc.check_game_update(sid)
    assert r["installed"]["build"] == "25247047" and r["installed"]["found"]
    assert r["status"] == "update_available" and r["latest"]["build"] == "25300000"
    assert svc.get_status(sid).installed_build == "25247047"
    mirror.build = "25247047"
    svc.steam._entries.clear()
    assert svc.check_game_update(sid)["status"] == "up_to_date"


def test_version_text_that_is_not_a_number_is_shown_but_not_compared(world):
    svc, sid, data, _ = world
    (data / "game").mkdir(exist_ok=True)
    (data / "game" / "current_version").write_text("0.7.1.2")
    r = svc.check_game_update(sid)
    assert r["installed"]["build"] == "0.7.1.2" and r["status"] == "could_not_check"
    assert "not a Steam build number" in r["summary"] and r["update_available"] is None


def test_a_steam_record_is_still_used_when_there_is_no_version_file(world):
    svc, sid, data, _ = world
    (data / "game" / "steamapps").mkdir(parents=True)
    (data / "game" / "steamapps" / "appmanifest_2278520.acf").write_text(steam.render_manifest("2278520", "25247047"))
    svc._manifest_missing.clear()
    assert svc.check_game_update(sid)["installed"]["build"] == "25247047"


def test_version_file_cannot_escape_the_data_folder(tmp_path, world):
    svc, sid, data, _ = world
    (tmp_path / "outside").write_text("111")
    (data / "game").mkdir(exist_ok=True)
    (data / "game" / "current_version").symlink_to(tmp_path / "outside")
    assert not svc.check_game_update(sid)["installed"]["found"]


def test_old_servers_pick_up_installed_build_file_from_the_catalog(world):
    svc, sid, data, _ = world
    inst = svc.store.get(sid)
    inst.template.pop("installed_build_file", None)
    svc.store.put(inst)
    (data / "game").mkdir(exist_ok=True)
    (data / "game" / "current_version").write_text("25247047")
    svc._manifest_missing.clear()
    assert svc.check_game_update(sid)["installed"]["build"] == "25247047"


# ------------------------------------------------------------------ the settings file (nested JSON form)
def fields(text=REAL_JSON):
    return {f.id: f for f in cf.build_form(text, CF).fields}


def test_nested_objects_and_arrays_of_objects_become_fields():
    f = fields()
    assert list(f)[:5] == ["name", "saveDirectory", "logDirectory", "ip", "queryPort"]
    assert f["gameSettings.playerHealthFactor"].label == "gameSettings.playerHealthFactor"
    assert f["gameSettings.playerHealthFactor"].group == "gameSettings"
    assert (f["gameSettings.enableDurability"].type, f["gameSettings.enableDurability"].value) == ("bool", "true")
    assert (f["gameSettings.foodBuffDurationFactor"].type, f["gameSettings.foodBuffDurationFactor"].value) == ("number", "1.5")
    assert f["gameSettings.tombstoneMode"].type == "text"
    assert f["userGroups[0].password"].group == "userGroups[0]" and f["userGroups[1].reservedSlots"].value == "2"
    assert f["userGroups[0].canKickBan"].type == "bool"
    assert (f["tags"].type, f["tags"].value) == ("json", "[]") and f["bannedAccounts"].value == "[]"   # empty arrays: raw JSON
    assert f["name"].label == "Name" and f["queryPort"].label == "Query Port" and f["name"].group == ""
    assert not any(k in f for k in ("gameSettings", "userGroups"))        # containers themselves are not fields
    assert f["userGroups[0].password"].sensitive and f["userGroups[1].password"].sensitive
    assert not f["gameSettings.tombstoneMode"].sensitive


def test_round_trip_is_byte_for_byte():
    f = fields()
    out, changed = cf.apply_changes(REAL_JSON, CF, {k: v.value for k, v in f.items()})
    assert out == REAL_JSON and changed == []


def test_editing_nested_values_changes_only_those_values():
    out, changed = cf.apply_changes(REAL_JSON, CF, {
        "gameSettings.playerHealthFactor": "2", "gameSettings.enableStarvingDebuff": "true",
        "gameSettings.tombstoneMode": "Everything", "userGroups[0].canKickBan": "true",
        "userGroups[1].password": "adminpw", "slotCount": "10", "tags": '["pve", "chill"]'})
    assert sorted(changed) == sorted(["gameSettings.playerHealthFactor", "gameSettings.enableStarvingDebuff",
                                      "gameSettings.tombstoneMode", "userGroups[0].canKickBan",
                                      "userGroups[1].password", "slotCount", "tags"])
    data = json.loads(out)
    assert data["gameSettings"]["playerHealthFactor"] == 2 and data["gameSettings"]["enableStarvingDebuff"] is True
    assert data["gameSettings"]["tombstoneMode"] == "Everything" and data["userGroups"][0]["canKickBan"] is True
    assert data["userGroups"][1]["password"] == "adminpw" and data["userGroups"][0]["password"] == "Xk3mQ9ra"
    assert data["slotCount"] == 10 and data["tags"] == ["pve", "chill"]
    assert data["gameSettings"]["miningDamageFactor"] == 1 and data["userGroups"][1]["reservedSlots"] == 2
    # nothing else moved: undoing the edits gives the original text back, so tabs and layout are untouched
    back, _ = cf.apply_changes(out, CF, {
        "gameSettings.playerHealthFactor": "1", "gameSettings.enableStarvingDebuff": "false",
        "gameSettings.tombstoneMode": "AddBackpackMaterials", "userGroups[0].canKickBan": "false",
        "userGroups[1].password": "", "slotCount": "16", "tags": "[]"})
    assert back == REAL_JSON
    assert "\t\t\t\"password\": \"adminpw\"," in out and "  " not in out


def test_nested_validation_names_the_full_path():
    with pytest.raises(Exception, match=r"gameSettings\.playerHealthFactor.*number"):
        cf.apply_changes(REAL_JSON, CF, {"gameSettings.playerHealthFactor": "lots"})
    with pytest.raises(Exception, match=r"userGroups\[0\]\.canKickBan.*true or false"):
        cf.apply_changes(REAL_JSON, CF, {"userGroups[0].canKickBan": "maybe"})
    with pytest.raises(Exception, match="Unknown setting"):
        cf.apply_changes(REAL_JSON, CF, {"userGroups[7].password": "x"})


def test_masked_passwords_in_nested_groups_are_kept_and_other_edits_still_work():
    masked = REAL_JSON.replace("Xk3mQ9ra", MASK)
    f = fields(masked)
    assert f["userGroups[0].password"].value == MASK
    out, changed = cf.apply_changes(masked, CF, {"userGroups[0].password": MASK, "gameSettings.playerManaFactor": "3"})
    assert changed == ["gameSettings.playerManaFactor"] and MASK in out


def test_other_json_shapes_still_work():
    assert cf.build_form("{}", CF).fields == ()
    assert cf.build_form("[1]", CF) is None and cf.build_form('{"a": 1,}', CF) is None
    f = fields('{"a": {"b": {"c": [{"d": 1}, {"e": [1, 2]}], "f": {}}}, "g": [1, {"h": 2}]}')
    assert list(f) == ["a.b.c[0].d", "a.b.c[1].e", "a.b.f", "g"]      # mixed arrays and empty objects stay raw JSON


def test_service_reads_and_saves_the_settings_file_through_the_form(world):
    svc, sid, data, _ = world
    p = data / SETTINGS_PATH
    p.parent.mkdir(parents=True)
    p.write_text(REAL_JSON)
    r = svc.read_config_file(sid, SETTINGS_PATH)
    assert r.editable_now and r.form.kind == "json"
    assert "Xk3mQ9ra" not in r.content and MASK in r.content and "Xk3mQ9ra" not in repr(r.form)
    by = {f.id: f for f in r.form.fields}
    assert by["userGroups[0].password"].value == MASK
    res = svc.config_form(sid, SETTINGS_PATH, changes={"gameSettings.playerHealthFactor": "2",
                                                       "userGroups[0].password": MASK})
    assert res.changed == ("gameSettings.playerHealthFactor",)
    svc.write_config_file(sid, SETTINGS_PATH, res.content)
    saved = p.read_text()
    assert saved == REAL_JSON.replace('"playerHealthFactor": 1,', '"playerHealthFactor": 2,')   # the real password is still there
    assert "Xk3mQ9ra" in saved


def test_settings_file_cannot_be_saved_while_the_server_runs(world):
    svc, sid, data, _ = world
    p = data / SETTINGS_PATH
    p.parent.mkdir(parents=True)
    p.write_text(REAL_JSON)
    svc.start(sid)
    r = svc.read_config_file(sid, SETTINGS_PATH)
    assert not r.editable_now and "Stop the server" in r.note
    with pytest.raises(Exception, match="Stop the server"):
        svc.write_config_file(sid, SETTINGS_PATH, REAL_JSON)
