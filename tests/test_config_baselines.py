"""Recorded defaults of config files (0.5.2): first-seen capture, never overwritten, what is stored (and what is
not), comparison by meaning, persistence after saving, new/missing settings, reset data flow, re-recording,
cleanup on delete/wipe, the web endpoint and the MCP tool."""
import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamedock import baselines as bl
from gamedock import configforms as forms
from gamedock.backends import FakeBackend
from gamedock.configfiles import MASK
from gamedock.errors import ValidationError
from gamedock.models import ConfigFile
from gamedock.service import GameDockService
from gamedock.web.app import create_app

from .conftest import StepClock
from .mcp_helpers import call, make_app, run

CATALOG = Path(__file__).resolve().parent.parent / "catalog"
PAL = "palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
ENS = "game/server/enshrouded_server.json"
INI = "conf/settings.ini"
ADMIN_SECRET = "pal-admin-s3cret-XYZ"
SRV_SECRET = "pal-join-pw-QRS"
GROUP_SECRET = "ens-group-pw-123"

PAL_TEXT = (
    "[/Script/Pal.PalGameWorldSettings]\n"
    'OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,DeathPenalty=All,bIsPvP=False,'
    'ServerPlayerMaxNum=16,ServerName="My Palworld Server",ServerDescription="",'
    f'AdminPassword="{ADMIN_SECRET}",ServerPassword="",CrossplayPlatforms=(Steam,Xbox),RCONEnabled=False)\n'
)

ENS_DOC = {
    "name": "weber brothers", "saveDirectory": "./savegame", "queryPort": 15637, "slotCount": 3, "tags": [],
    "voiceChatMode": "Proximity", "enableVoiceChat": False,
    "gameSettings": {"playerHealthFactor": 1, "enableDurability": True, "miningDamageFactor": 1.0,
                     "weatherFrequency": "Normal"},
    "userGroups": [{"name": "Default", "password": GROUP_SECRET, "canKickBan": False, "reservedSlots": 0}],
    "bannedAccounts": [1, 2.0],
}


def ens_text(doc=None):
    return json.dumps(doc or ENS_DOC, indent="\t") + "\n"


@pytest.fixture
def svc(tmp_path):
    return GameDockService(FakeBackend(), CATALOG, tmp_path / "data", clock=StepClock())


@pytest.fixture
def pal(svc, tmp_path):
    svc.deploy("palworld", "Family Palworld", env={"ADMIN_PASSWORD": "x" * 10})
    return "family-palworld"


@pytest.fixture
def ens(svc, tmp_path):
    svc.deploy("enshrouded", "Weber Brothers")
    return "weber-brothers"


def put(tmp_path, sid, rel, text):
    p = tmp_path / "data" / sid / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def bfile(tmp_path, sid, rel):
    import hashlib
    return tmp_path / "data" / "config-baselines" / sid / (hashlib.sha1(rel.encode()).hexdigest() + ".json")


def field(form, fid):
    return next(f for f in form.fields if f.id == fid)


def edit(svc, sid, rel, changes):
    """What the web page does on Save: build the new text from the file on disk, then write it."""
    res = svc.config_form(sid, rel, changes=changes)
    svc.write_config_file(sid, rel, res.content)


# ------------------------------------------------------------------ first-seen capture
def test_first_read_records_the_baseline_once(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    assert not bfile(tmp_path, pal, PAL).exists()
    r = svc.read_config_file(pal, PAL)
    assert r.baseline.available and r.baseline.just_recorded and r.baseline.origin == "first-seen"
    assert r.form.differing == 0 and all(f.status == "same" and not f.differs for f in r.form.fields)
    r2 = svc.read_config_file(pal, PAL)
    assert r2.baseline.available and not r2.baseline.just_recorded and r2.baseline.captured_at == r.baseline.captured_at
    d = json.loads(bfile(tmp_path, pal, PAL).read_text())
    assert d["version"] == 1 and d["path"] == PAL and d["origin"] == "first-seen" and d["captured_at"].endswith("Z")
    assert d["source_size"] == len(PAL_TEXT.encode()) and d["source_mtime"] > 0
    assert d["kind"] == "optionsettings" and "OptionSettings" not in json.dumps(d["fields"][0])  # parsed list, no file text
    assert [f["id"] for f in d["fields"]][:3] == ["Difficulty", "DayTimeSpeedRate", "DeathPenalty"]


def test_baseline_file_mode_location_and_no_leftovers(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    svc.read_config_file(pal, PAL)
    f = bfile(tmp_path, pal, PAL)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert sorted(p.name for p in f.parent.iterdir()) == [f.name]  # no temp files left behind
    server_data = tmp_path / "data" / pal
    assert not any("baseline" in str(p.relative_to(server_data)) for p in server_data.rglob("*"))  # nothing inside the game's own folder
    assert "family-palworld" in str(f) and "Family" not in str(f)          # keyed by the server id, not the name


def test_no_file_no_baseline_until_it_appears(svc, pal, tmp_path):
    r = svc.read_config_file(pal, PAL)
    assert r.form is None and not r.baseline.available
    assert not (tmp_path / "data" / "config-baselines").exists()
    put(tmp_path, pal, PAL, PAL_TEXT)
    r = svc.read_config_file(pal, PAL)
    assert r.baseline.available and r.baseline.just_recorded


def test_empty_option_settings_is_not_a_baseline(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=()\n")
    r = svc.read_config_file(pal, PAL)
    assert not r.baseline.available and not bfile(tmp_path, pal, PAL).exists()
    put(tmp_path, pal, PAL, PAL_TEXT)
    assert svc.read_config_file(pal, PAL).baseline.just_recorded


def test_a_file_without_a_form_has_no_baseline(service, deployed, tmp_path):
    p = tmp_path / "data" / "one" / "conf" / "hot.txt"      # a plain text file: raw view only
    p.parent.mkdir(parents=True)
    p.write_text("just some text\n")
    r = service.read_config_file("one", "conf/hot.txt")
    assert r.form is None and not r.baseline.available
    assert not (tmp_path / "data" / "config-baselines").exists()


def test_the_baseline_is_never_overwritten(svc, pal, tmp_path):
    p = put(tmp_path, pal, PAL, PAL_TEXT)
    svc.read_config_file(pal, PAL)
    before = bfile(tmp_path, pal, PAL).read_bytes()
    p.write_text(PAL_TEXT.replace("ServerPlayerMaxNum=16", "ServerPlayerMaxNum=32"))   # edited by hand elsewhere
    for _ in range(3):
        r = svc.read_config_file(pal, PAL)
        svc.config_form(pal, PAL)
    assert bfile(tmp_path, pal, PAL).read_bytes() == before
    f = field(r.form, "ServerPlayerMaxNum")
    assert f.differs and f.status == "changed" and f.default == "16" and f.value == "32"
    assert r.form.differing == 1 and not r.baseline.just_recorded


def test_save_without_a_prior_read_records_the_file_as_it_was(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    svc.write_config_file(pal, PAL, PAL_TEXT.replace("ServerPlayerMaxNum=16", "ServerPlayerMaxNum=8"))
    d = json.loads(bfile(tmp_path, pal, PAL).read_text())
    assert d["origin"] == "pre-save"
    assert next(x for x in d["fields"] if x["id"] == "ServerPlayerMaxNum")["value"] == "16"
    r = svc.read_config_file(pal, PAL)
    assert field(r.form, "ServerPlayerMaxNum").default == "16" and field(r.form, "ServerPlayerMaxNum").differs
    assert not r.baseline.just_recorded


def test_a_form_preview_never_records_the_edited_text(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    res = svc.config_form(pal, PAL, content=PAL_TEXT.replace("ServerPlayerMaxNum=16", "ServerPlayerMaxNum=99"))
    assert res.baseline.just_recorded                       # taken from the file on disk, not from the sent text
    d = json.loads(bfile(tmp_path, pal, PAL).read_text())
    assert next(x for x in d["fields"] if x["id"] == "ServerPlayerMaxNum")["value"] == "16"
    assert field(res.form, "ServerPlayerMaxNum").differs    # the working copy is compared with it


# ------------------------------------------------------------------ secrets
def test_secrets_are_never_in_the_baseline_file(svc, pal, ens, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT.replace('ServerPassword=""', f'ServerPassword="{SRV_SECRET}"'))
    put(tmp_path, ens, ENS, ens_text())
    svc.read_config_file(pal, PAL)
    svc.read_config_file(ens, ENS)
    for sid, rel, secrets in ((pal, PAL, (ADMIN_SECRET, SRV_SECRET)), (ens, ENS, (GROUP_SECRET,))):
        raw = bfile(tmp_path, sid, rel).read_text()
        for s in secrets:
            assert s not in raw
    d = json.loads(bfile(tmp_path, pal, PAL).read_text())
    adm = next(x for x in d["fields"] if x["id"] == "AdminPassword")
    assert adm["sensitive"] and adm["value"] is None and adm["set"] is True
    assert next(x for x in d["fields"] if x["id"] == "ServerPassword")["set"] is True
    pw = next(x for x in json.loads(bfile(tmp_path, ens, ENS).read_text())["fields"] if x["id"] == "userGroups[0].password")
    assert pw["value"] is None and pw["set"] is True
    # and nowhere else under the data folder's baseline area
    blob = b"".join(p.read_bytes() for p in (tmp_path / "data" / "config-baselines").rglob("*") if p.is_file())
    assert ADMIN_SECRET.encode() not in blob and SRV_SECRET.encode() not in blob and GROUP_SECRET.encode() not in blob


def test_secret_markers_differ_only_when_the_set_state_changes_or_a_new_value_is_typed(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)                       # AdminPassword set, ServerPassword empty
    r = svc.read_config_file(pal, PAL)
    admin, join = field(r.form, "AdminPassword"), field(r.form, "ServerPassword")
    assert admin.sensitive and admin.default is None and admin.default_set is True and not admin.differs
    assert join.default_set is False and not join.differs
    # the user types a new value this session (preview of the working copy)
    res = svc.config_form(pal, PAL, changes={"AdminPassword": "brand-new-pw", "ServerPassword": "joinme"})
    assert field(res.form, "AdminPassword").differs and field(res.form, "ServerPassword").differs
    assert "brand-new-pw" not in bfile(tmp_path, pal, PAL).read_text()
    # saved: ServerPassword went from not set to set -> still differs; AdminPassword stays set -> no marker
    edit(svc, pal, PAL, {"AdminPassword": "brand-new-pw", "ServerPassword": "joinme"})
    r = svc.read_config_file(pal, PAL)
    assert field(r.form, "ServerPassword").differs and field(r.form, "ServerPassword").status == "changed"
    assert not field(r.form, "AdminPassword").differs      # secrets are not compared by value
    # clearing the admin password changes its set state
    edit(svc, pal, PAL, {"AdminPassword": ""})
    r = svc.read_config_file(pal, PAL)
    assert field(r.form, "AdminPassword").differs and field(r.form, "AdminPassword").default_set is True
    assert "brand-new-pw" not in json.dumps(r.content) and "joinme" not in json.dumps(r.content)


# ------------------------------------------------------------------ persistence after save
def test_markers_survive_save_and_restart(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    svc.read_config_file(pal, PAL)
    res = svc.config_form(pal, PAL, changes={"ServerPlayerMaxNum": "24", "bIsPvP": "true", "ServerName": "Weber Pals"})
    assert {f.id for f in res.form.fields if f.differs} == {"bIsPvP", "ServerPlayerMaxNum", "ServerName"}
    svc.write_config_file(pal, PAL, res.content)
    for s in (svc, GameDockService(FakeBackend(), CATALOG, tmp_path / "data", clock=StepClock())):   # same after a restart
        r = s.read_config_file(pal, PAL)
        assert r.form.differing == 3 and not r.baseline.just_recorded
        got = {f.id: (f.default, f.value, f.status) for f in r.form.fields if f.differs}
        assert got == {"ServerPlayerMaxNum": ("16", "24", "changed"), "bIsPvP": ("False", "True", "changed"),
                       "ServerName": ("My Palworld Server", "Weber Pals", "changed")}
        assert [f.id for f in r.form.fields if not f.differs and f.status != "same"] == []


def test_enshrouded_nested_json_defaults(svc, ens, tmp_path):
    put(tmp_path, ens, ENS, ens_text())
    svc.read_config_file(ens, ENS)
    edit(svc, ens, ENS, {"gameSettings.playerHealthFactor": "2", "gameSettings.enableDurability": "false",
                         "slotCount": "4", "userGroups[0].canKickBan": "true"})
    r = svc.read_config_file(ens, ENS)
    got = {f.id: (f.default, f.value) for f in r.form.fields if f.differs}
    assert got == {"gameSettings.playerHealthFactor": ("1", "2"), "gameSettings.enableDurability": ("true", "false"),
                   "slotCount": ("3", "4"), "userGroups[0].canKickBan": ("false", "true")}
    assert r.form.differing == 4
    assert not field(r.form, "gameSettings.miningDamageFactor").differs


# ------------------------------------------------------------------ comparing by meaning
@pytest.mark.parametrize("a,b,typ", [
    ("1", "1.0", "number"), ("1.000000", "1", "number"), ("1.0", "1.000000", "number"), ("-0.50", "-.5", "number"),
    ("1800000000000", "1.8e12", "number"), ("0", "0.0", "number"), ("10", "1E+1", "number"),
    ("True", "true", "bool"), ("FALSE", "False", "bool"), ("  true ", "True", "bool"),
    ("hello", " hello  ", "text"),
    ('[1, 2.0]', '[1,2]', "json"), ('{"a": 1, "b": [true]}', '{"b":[true],"a":1.0}', "json"),
])
def test_values_that_mean_the_same_are_not_different(a, b, typ):
    assert bl.same_value(a, typ, b, typ)
    assert bl.same_value(b, typ, a, typ)


@pytest.mark.parametrize("a,b,typ", [
    ("1", "2", "number"), ("1.5", "1.50001", "number"), ("True", "False", "bool"), ("a", "A", "text"),
    ("1.0", "1", "text"), ("[true]", "[1]", "json"), ("[1,2]", "[2,1]", "json"), ('{"a":1}', '{"a":"1"}', "json"),
    ("", "0", "number"),
])
def test_values_that_differ_are_different(a, b, typ):
    assert not bl.same_value(a, typ, b, typ)


def test_reformatting_by_the_game_is_not_a_change(svc, pal, ens, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    put(tmp_path, ens, ENS, ens_text())
    svc.read_config_file(pal, PAL)
    svc.read_config_file(ens, ENS)
    # the game rewrites the same settings in another style
    put(tmp_path, pal, PAL, PAL_TEXT.replace("DayTimeSpeedRate=1.000000", "DayTimeSpeedRate=1")
        .replace("bIsPvP=False", "bIsPvP=false").replace("ServerPlayerMaxNum=16", "ServerPlayerMaxNum=16.0"))
    doc = json.loads(ens_text())
    doc["gameSettings"]["playerHealthFactor"] = 1.0
    doc["gameSettings"]["miningDamageFactor"] = 1
    doc["bannedAccounts"] = [1, 2]
    put(tmp_path, ens, ENS, json.dumps(doc, indent=2))
    assert svc.read_config_file(pal, PAL).form.differing == 0
    assert svc.read_config_file(ens, ENS).form.differing == 0


def test_text_and_number_types_are_compared_loosely_when_the_file_changes_style(svc, tmp_path):
    # types differ on the two sides: decided by what the text looks like
    assert bl.same_value('1.0', "text", "1", "number")
    assert not bl.same_value('abc', "text", "1", "number")


# ------------------------------------------------------------------ new / missing
def test_new_and_missing_settings(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    svc.read_config_file(pal, PAL)
    # a game update adds one setting and drops another
    put(tmp_path, pal, PAL, PAL_TEXT.replace("DeathPenalty=All,", "").replace("RCONEnabled=False)", "RCONEnabled=False,BrandNew=7)"))
    r = svc.read_config_file(pal, PAL)
    new = field(r.form, "BrandNew")
    assert new.status == "new" and new.differs and new.default is None
    assert [m.id for m in r.form.missing] == ["DeathPenalty"]
    m = r.form.missing[0]
    assert (m.status, m.default, m.label) == ("missing", "All", "Death Penalty")
    assert r.form.differing == 2
    ch = svc.config_changes(pal, PAL)
    assert {c["id"]: c["status"] for c in ch["changes"]} == {"BrandNew": "new", "DeathPenalty": "missing"}


# ------------------------------------------------------------------ reset buttons (backend side of the data flow)
def test_reset_to_default_and_reset_all_fill_the_form_only(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    svc.read_config_file(pal, PAL)
    edit(svc, pal, PAL, {"ServerPlayerMaxNum": "24", "bIsPvP": "true", "DayTimeSpeedRate": "2.5"})
    on_disk = (tmp_path / "data" / pal / PAL).read_text()
    r = svc.read_config_file(pal, PAL)
    assert r.form.differing == 3
    # "Reset to default" for one field: the page sends that field's default as a change
    one = svc.config_form(pal, PAL, changes={"bIsPvP": field(r.form, "bIsPvP").default})
    assert field(one.form, "bIsPvP").status == "same" and one.form.differing == 2
    assert (tmp_path / "data" / pal / PAL).read_text() == on_disk              # nothing was written
    # "Reset all": every differing field's default
    allc = {f.id: f.default for f in r.form.fields if f.differs and f.default is not None}
    res = svc.config_form(pal, PAL, changes=allc)
    assert res.form.differing == 0 and (tmp_path / "data" / pal / PAL).read_text() == on_disk
    svc.write_config_file(pal, PAL, res.content)                               # only Save writes
    r = svc.read_config_file(pal, PAL)
    assert r.form.differing == 0
    # the default's own spelling is restored for numbers: 1.000000 stays equal to what the game first wrote
    assert "ServerPlayerMaxNum=16" in (tmp_path / "data" / pal / PAL).read_text()


# ------------------------------------------------------------------ re-recording
def test_record_current_settings_as_the_default(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    svc.read_config_file(pal, PAL)
    edit(svc, pal, PAL, {"ServerPlayerMaxNum": "24"})
    old = bfile(tmp_path, pal, PAL).read_bytes()
    with pytest.raises(ValidationError, match="confirmation"):
        svc.record_config_baseline(pal, PAL)
    assert bfile(tmp_path, pal, PAL).read_bytes() == old
    r = svc.record_config_baseline(pal, PAL, confirm=True)
    assert r.form.differing == 0 and r.baseline.origin == "manual" and r.baseline.has_previous
    prev = bfile(tmp_path, pal, PAL).with_name(bfile(tmp_path, pal, PAL).name[:-5] + ".prev.json")
    assert prev.read_bytes() == old and stat.S_IMODE(prev.stat().st_mode) == 0o600
    assert field(r.form, "ServerPlayerMaxNum").default == "24"
    # a second re-record keeps exactly one previous level (the one before)
    edit(svc, pal, PAL, {"ServerPlayerMaxNum": "30"})
    svc.record_config_baseline(pal, PAL, confirm=True)
    assert len([p for p in prev.parent.iterdir()]) == 2
    assert json.loads(prev.read_text())["origin"] == "manual"


def test_record_needs_a_file_with_a_form(svc, pal, tmp_path):
    with pytest.raises(ValidationError, match="not created"):
        svc.record_config_baseline(pal, PAL, confirm=True)


# ------------------------------------------------------------------ deploy-time capture
def test_a_new_server_gets_its_baseline_when_it_becomes_ready(service, deployed, backend, tmp_path):
    p = tmp_path / "data" / "one" / INI
    p.parent.mkdir(parents=True)
    p.write_text("Name=Hello\nslots=8\n")
    assert not (tmp_path / "data" / "config-baselines").exists()
    service.start("one")
    assert service.get_status("one").ready
    d = json.loads(next((tmp_path / "data" / "config-baselines" / "one").iterdir()).read_text())
    assert d["origin"] == "deploy" and d["path"] == INI
    # edited afterwards: the next look does not change the baseline, and the dialog shows no "just recorded" notice
    p.write_text("Name=Hello\nslots=12\n")
    service.get_status("one")
    r = service.read_config_file("one", INI)
    assert not r.baseline.just_recorded and field(r.form, "slots").default == "8" and field(r.form, "slots").differs


def test_deploy_time_capture_waits_for_the_file_then_only_looks_for_new_servers(service, deployed, tmp_path, monkeypatch):
    service.start("one")
    service.BASELINE_DEPLOY_RETRY = 0.0
    service.get_status("one")                                     # no file yet: nothing recorded, no error
    assert not (tmp_path / "data" / "config-baselines").exists()
    p = tmp_path / "data" / "one" / INI
    p.parent.mkdir(parents=True)
    p.write_text("a=1\n")
    service.get_status("one")
    assert (tmp_path / "data" / "config-baselines" / "one").is_dir()


def test_an_old_server_is_not_recorded_silently(service, deployed, tmp_path):
    from dataclasses import replace
    service.store.put(replace(service.store.get("one"), created_at="2026-01-01T00:00:00+00:00"))
    p = tmp_path / "data" / "one" / INI
    p.parent.mkdir(parents=True)
    p.write_text("a=1\n")
    service.start("one")
    service.get_status("one")
    assert not (tmp_path / "data" / "config-baselines").exists()
    assert service.read_config_file("one", INI).baseline.just_recorded   # first look is when the dialog opens


# ------------------------------------------------------------------ cleanup
def test_baselines_follow_the_data_folder(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    svc.read_config_file(pal, PAL)
    folder = tmp_path / "data" / "config-baselines" / pal
    svc.delete(pal, confirm=True)                                   # data folder kept -> its recorded defaults stay valid
    assert folder.is_dir()
    svc.deploy("palworld", "Family Palworld", env={"ADMIN_PASSWORD": "x" * 10})
    assert not svc.read_config_file(pal, PAL).baseline.just_recorded
    svc.delete(pal, confirm=True, delete_data=True, confirm_delete_data=True)
    assert not folder.exists() and not (tmp_path / "data" / pal).exists()
    # the safety backup holds the files, not the GameDock-side record
    assert not any("config-baselines" in n for p in (tmp_path / "data" / "backups").rglob("*.tar*") for n in [p.name])


def test_wipe_data_forgets_the_defaults(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    svc.read_config_file(pal, PAL)
    svc.wipe_data(pal, confirm=True)
    assert not (tmp_path / "data" / "config-baselines" / pal).exists()
    put(tmp_path, pal, PAL, PAL_TEXT.replace("ServerPlayerMaxNum=16", "ServerPlayerMaxNum=5"))
    r = svc.read_config_file(pal, PAL)
    assert r.baseline.just_recorded and field(r.form, "ServerPlayerMaxNum").default == "5"


def test_the_baseline_folder_name_is_reserved(svc):
    assert svc.deploy("palworld", "Config Baselines", env={"ADMIN_PASSWORD": "x" * 10}).id == "config-baselines-2"


def test_a_damaged_baseline_is_replaced_and_never_breaks_the_form(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    svc.read_config_file(pal, PAL)
    bfile(tmp_path, pal, PAL).write_text("{not json")
    r = svc.read_config_file(pal, PAL)
    assert r.form is not None and r.baseline.just_recorded and r.form.differing == 0


# ------------------------------------------------------------------ web endpoint
def test_endpoints_carry_the_markers_and_rerecord(svc, pal, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    c = TestClient(create_app(svc))
    r = c.get(f"/api/servers/{pal}/config/file", params={"path": PAL}).json()
    assert r["baseline"]["available"] and r["baseline"]["just_recorded"] and r["baseline"]["captured_at"]
    assert ADMIN_SECRET not in json.dumps(r)
    f = next(x for x in r["form"]["fields"] if x["id"] == "ServerPlayerMaxNum")
    assert f["default"] == "16" and f["differs"] is False and f["status"] == "same" and r["form"]["differing"] == 0
    p = c.post(f"/api/servers/{pal}/config/form", json={"path": PAL, "changes": {"ServerPlayerMaxNum": "20"}}).json()
    f = next(x for x in p["form"]["fields"] if x["id"] == "ServerPlayerMaxNum")
    assert f["differs"] and f["status"] == "changed" and f["default"] == "16" and p["form"]["differing"] == 1
    assert p["baseline"]["available"] and not p["baseline"]["just_recorded"] and p["form"]["missing"] == []
    saved = c.put(f"/api/servers/{pal}/config/file", json={"path": PAL, "content": p["content"]})
    assert saved.status_code == 200
    r = c.get(f"/api/servers/{pal}/config/file", params={"path": PAL}).json()
    assert r["form"]["differing"] == 1
    bad = c.post(f"/api/servers/{pal}/config/baseline", json={"path": PAL})
    assert bad.status_code == 400 and "confirmation" in bad.json()["error"]
    ok = c.post(f"/api/servers/{pal}/config/baseline", json={"path": PAL, "confirm": True}).json()
    assert ok["form"]["differing"] == 0 and ok["baseline"]["origin"] == "manual" and ok["baseline"]["has_previous"]
    assert c.post("/api/servers/nope/config/baseline", json={"path": PAL, "confirm": True}).status_code == 404


# ------------------------------------------------------------------ MCP
def test_mcp_get_config_changes(svc, pal, ens, tmp_path):
    put(tmp_path, pal, PAL, PAL_TEXT)
    put(tmp_path, ens, ENS, ens_text())
    svc.read_config_file(pal, PAL)
    edit(svc, pal, PAL, {"ServerPlayerMaxNum": "24", "ServerPassword": "joinme", "AdminPassword": ""})
    first = svc.config_changes(ens, ENS)                       # nothing recorded yet: recorded now, nothing can differ
    assert first["baseline"]["just_recorded"] and first["count"] == 0 and "just now" in first["note"]

    async def go(c):
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["get_config_changes"].annotations.read_only_hint is True
        a = await call(c, "get_config_changes", server_id=pal, path=PAL)
        b = await call(c, "get_config_changes", server_id=ens, path=ENS)
        bad = await call(c, "get_config_changes", server_id=pal, path="nope.ini")
        return a, b, bad

    for mode in ("read_only", "standard", "full"):
        (ea, a), (eb, b), (eb2, _) = run(make_app(svc, mode), go)
        assert not ea and not eb and eb2
        ch = {x["id"]: x for x in a["changes"]}
        assert set(ch) == {"ServerPlayerMaxNum", "ServerPassword", "AdminPassword"} and a["count"] == 3
        assert (ch["ServerPlayerMaxNum"]["default"], ch["ServerPlayerMaxNum"]["current"]) == ("16", "24")
        assert (ch["ServerPassword"]["default"], ch["ServerPassword"]["current"]) == ("not set", "set")
        assert (ch["AdminPassword"]["default"], ch["AdminPassword"]["current"]) == ("set", "not set")
        text = json.dumps(a)
        assert "joinme" not in text and ADMIN_SECRET not in text and MASK not in text
        assert a["baseline"]["available"] and a["baseline"]["origin"] == "first-seen"
        assert b["count"] == 0 and b["baseline"]["available"] and GROUP_SECRET not in json.dumps(b)


def test_mcp_tool_is_not_in_off_mode_and_is_in_the_guide(svc):
    async def guide(c):
        return (await c.read_resource("gamedock://guide")).contents[0].text

    g = run(make_app(svc, "read_only"), guide)
    assert "get_config_changes" in g and "no tool for it" in g
    with TestClient(make_app(svc, "off")) as c:
        assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                      headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}).status_code == 404


def test_mcp_has_no_tool_to_rerecord_defaults(svc):
    async def names(c):
        return {t.name for t in (await c.list_tools()).tools}

    n = run(make_app(svc, "full"), names)
    assert not [x for x in n if "baseline" in x or "default" in x]
