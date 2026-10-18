"""The structured (form) view of config files: parsing, labels, minimal edits, round trips, and the safety rules."""
import json

import pytest
from fastapi.testclient import TestClient

from gamedock import configforms as cf
from gamedock.configfiles import MASK
from gamedock.errors import ValidationError
from gamedock.models import ConfigFile
from gamedock.web.app import create_app

from .conftest import ADMIN

PAL = ConfigFile("palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini", format="ini",
                 sensitive_keys=("AdminPassword", "ServerPassword"))
PAL_TEXT = (
    "; This configuration file is a sample, copy it to PalWorldSettings.ini\n"
    "[/Script/Pal.PalGameWorldSettings]\n"
    'OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,DeathPenalty=All,bIsPvP=False,'
    'ServerPlayerMaxNum=32,ServerName="Default Palworld Server",ServerDescription="",'
    'AdminPassword="",ServerPassword="",CrossplayPlatforms=(Steam,Xbox,PS5,Mac),RCONEnabled=False)\n'
)


def fields(text, conf=PAL):
    f = cf.build_form(text, conf)
    assert f is not None
    return {x.id: x for x in f.fields}


# ------------------------------------------------------------------ labels
@pytest.mark.parametrize("key,label", [
    ("ServerPlayerMaxNum", "Server Player Max Num"), ("max-players", "Max Players"), ("level_name", "Level Name"),
    ("bIsPvP", "Is PvP"), ("RCONEnabled", "RCON Enabled"), ("view-distance", "View Distance"),
    ("a.b.c", "A B C"), ("x", "X"),
])
def test_humanize(key, label):
    assert cf.humanize(key) == label


# ------------------------------------------------------------------ OptionSettings (Palworld style)
def test_optionsettings_fields_and_types():
    f = fields(PAL_TEXT)
    assert list(f) == ["Difficulty", "DayTimeSpeedRate", "DeathPenalty", "bIsPvP", "ServerPlayerMaxNum", "ServerName",
                       "ServerDescription", "AdminPassword", "ServerPassword", "CrossplayPlatforms", "RCONEnabled"]
    assert (f["bIsPvP"].type, f["bIsPvP"].value) == ("bool", "False")
    assert (f["ServerPlayerMaxNum"].type, f["ServerPlayerMaxNum"].value) == ("number", "32")
    assert (f["DayTimeSpeedRate"].type, f["DayTimeSpeedRate"].value) == ("number", "1.000000")
    assert (f["ServerName"].type, f["ServerName"].value) == ("text", "Default Palworld Server")  # quotes are not part of the value
    assert f["CrossplayPlatforms"].value == "(Steam,Xbox,PS5,Mac)"
    assert f["Difficulty"].value == "None" and f["Difficulty"].type == "text"
    assert f["AdminPassword"].sensitive and f["ServerPassword"].sensitive and not f["ServerName"].sensitive
    assert f["ServerPlayerMaxNum"].label == "Server Player Max Num"


def test_optionsettings_is_detected_even_without_ini_hint():
    f = cf.build_form(PAL_TEXT, ConfigFile("whatever.txt", format="text"))
    assert f is not None and f.kind == "optionsettings"


def test_quoted_strings_with_commas_and_parentheses():
    text = 'OptionSettings=(A=1,ServerName="a, b (c, d)",B=2,ServerDescription="x),y=(z",C=True)\n'
    f = fields(text)
    assert list(f) == ["A", "ServerName", "B", "ServerDescription", "C"]
    assert f["ServerName"].value == "a, b (c, d)"
    assert f["ServerDescription"].value == "x),y=(z"
    assert f["B"].value == "2" and f["C"].value == "True"


def test_escaped_quotes_inside_a_string():
    text = r'OptionSettings=(ServerName="say \"hi\", ok",B=1)' + "\n"
    f = fields(text)
    assert f["ServerName"].value == 'say "hi", ok' and f["B"].value == "1"
    out, _ = cf.apply_changes(text, PAL, {"ServerName": 'new "name", here'})
    assert out == r'OptionSettings=(ServerName="new \"name\", here",B=1)' + "\n"
    assert fields(out)["ServerName"].value == 'new "name", here'


def test_changing_one_value_changes_nothing_else():
    out, changed = cf.apply_changes(PAL_TEXT, PAL, {"ServerPlayerMaxNum": "16"})
    assert changed == ["ServerPlayerMaxNum"]
    assert out == PAL_TEXT.replace("ServerPlayerMaxNum=32", "ServerPlayerMaxNum=16")


def test_unchanged_values_round_trip_byte_for_byte():
    every = {k: v.value for k, v in fields(PAL_TEXT).items()}
    out, changed = cf.apply_changes(PAL_TEXT, PAL, every)
    assert out == PAL_TEXT and changed == []


def test_edit_every_kind_of_field():
    out, changed = cf.apply_changes(PAL_TEXT, PAL, {
        "ServerName": "a, b", "bIsPvP": "true", "Difficulty": "Hard", "ServerDescription": "hello",
        "DayTimeSpeedRate": "2.5", "CrossplayPlatforms": "(Steam,Mac)"})
    assert sorted(changed) == ["CrossplayPlatforms", "DayTimeSpeedRate", "Difficulty", "ServerDescription",
                               "ServerName", "bIsPvP"]
    f = fields(out)
    assert f["ServerName"].value == "a, b" and 'ServerName="a, b"' in out        # quoted style is kept
    assert f["bIsPvP"].value == "True"                                             # the file's spelling, not "true"
    assert f["Difficulty"].value == "Hard" and "Difficulty=Hard," in out         # bare stays bare
    assert f["ServerDescription"].value == "hello"
    assert f["DayTimeSpeedRate"].value == "2.5"
    assert f["CrossplayPlatforms"].value == "(Steam,Mac)"
    assert out.startswith("; This configuration file is a sample")
    assert f["ServerPlayerMaxNum"].value == "32"  # untouched


def test_bare_value_that_would_break_the_line_gets_quoted():
    out, _ = cf.apply_changes(PAL_TEXT, PAL, {"Difficulty": "Very, Hard"})
    assert 'Difficulty="Very, Hard"' in out and fields(out)["Difficulty"].value == "Very, Hard"
    assert len(fields(out)) == len(fields(PAL_TEXT))  # the comma did not create a new setting


def test_validation_of_typed_values():
    with pytest.raises(ValidationError, match="number"):
        cf.apply_changes(PAL_TEXT, PAL, {"ServerPlayerMaxNum": "lots"})
    with pytest.raises(ValidationError, match="True or False"):
        cf.apply_changes(PAL_TEXT, PAL, {"bIsPvP": "maybe"})
    with pytest.raises(ValidationError, match="line break"):
        cf.apply_changes(PAL_TEXT, PAL, {"ServerName": "a\nb"})
    with pytest.raises(ValidationError, match="Unknown setting"):
        cf.apply_changes(PAL_TEXT, PAL, {"Nope": "1"})


def test_multiline_option_settings_and_crlf_files_are_handled():
    text = 'OptionSettings=(\r\n  A=1,\r\n  B="x, y",\r\n  C=False\r\n)\r\n'
    f = fields(text)
    assert list(f) == ["A", "B", "C"] and f["B"].value == "x, y"
    out, _ = cf.apply_changes(text, PAL, {"C": "True"})
    assert out == text.replace("C=False", "C=True")


@pytest.mark.parametrize("text", [
    'OptionSettings=(A=1,B="never closed)\n',
    "OptionSettings=(A=1,B=2\n",
])
def test_broken_option_settings_have_no_form(text):
    assert cf.build_form(text, PAL) is None


def test_masked_secrets_are_never_overwritten_by_the_placeholder():
    masked = PAL_TEXT.replace('AdminPassword=""', f'AdminPassword="{MASK}"')
    f = fields(masked)
    assert f["AdminPassword"].value == MASK and f["AdminPassword"].sensitive
    out, changed = cf.apply_changes(masked, PAL, {"AdminPassword": MASK, "ServerPlayerMaxNum": "20"})
    assert changed == ["ServerPlayerMaxNum"] and f"AdminPassword=\"{MASK}\"" in out
    out, changed = cf.apply_changes(masked, PAL, {"AdminPassword": "new pw, 1"})
    assert changed == ["AdminPassword"] and 'AdminPassword="new pw, 1"' in out


# ------------------------------------------------------------------ key=value files
MC = ConfigFile("server.properties", format="ini", sensitive_keys=("rcon.password",))
MC_TEXT = (
    "#Minecraft server properties\n#Sat Oct 17 20:00:00 UTC 2026\n"
    "enable-command-block=false\ngamemode=survival\nlevel-seed=\nmax-players=20\n"
    "motd=A Minecraft Server: come, play!\nrcon.password=hunter2\nonline-mode=TRUE\nview-distance=10\n"
)


def test_properties_fields():
    f = fields(MC_TEXT, MC)
    assert list(f) == ["enable-command-block", "gamemode", "level-seed", "max-players", "motd", "rcon.password",
                       "online-mode", "view-distance"]
    assert (f["enable-command-block"].type, f["enable-command-block"].value) == ("bool", "false")
    assert (f["max-players"].type, f["max-players"].value) == ("number", "20")
    assert f["level-seed"].value == "" and f["level-seed"].type == "text"
    assert f["motd"].value == "A Minecraft Server: come, play!"
    assert f["rcon.password"].sensitive
    assert f["max-players"].label == "Max Players"


def test_properties_edits_keep_comments_and_spelling():
    out, changed = cf.apply_changes(MC_TEXT, MC, {
        "max-players": "8", "enable-command-block": "true", "online-mode": "false", "motd": "Hi there",
        "level-seed": "12345"})
    assert sorted(changed) == ["enable-command-block", "level-seed", "max-players", "motd", "online-mode"]
    assert out == (MC_TEXT.replace("max-players=20", "max-players=8").replace("enable-command-block=false", "enable-command-block=true")
                   .replace("online-mode=TRUE", "online-mode=FALSE")
                   .replace("motd=A Minecraft Server: come, play!", "motd=Hi there")
                   .replace("level-seed=\n", "level-seed=12345\n"))


def test_properties_round_trip_is_exact_even_with_odd_spacing_and_crlf():
    text = "# c\r\nkey1 = value one  \r\n  key2=2\r\n\r\nkey3= true\r\n"
    f = fields(text, MC)
    assert list(f) == ["key1", "key2", "key3"] and f["key1"].value == "value one"
    out, changed = cf.apply_changes(text, MC, {k: v.value for k, v in f.items()})
    assert out == text and changed == []
    out, _ = cf.apply_changes(text, MC, {"key1": "x"})
    assert out == text.replace("value one", "x")


def test_ini_sections_and_duplicate_keys():
    ini = ConfigFile("conf/server.ini", format="ini")
    text = "[General]\nname=Alpha\nport=1\n[Network]\nport=2\nname=Beta\n; comment\n"
    f = fields(text, ini)
    assert list(f) == ["[General]name", "[General]port", "[Network]port", "[Network]name"]
    assert f["[Network]port"].group == "Network" and f["[Network]port"].key == "port"
    out, _ = cf.apply_changes(text, ini, {"[Network]port": "99"})
    assert out == text.replace("[Network]\nport=2", "[Network]\nport=99")
    dup = fields("a=1\na=2\n", MC)
    assert list(dup) == ["a", "a#2"]


def test_quoted_ini_values_keep_their_quotes():
    ini = ConfigFile("x.ini", format="ini")
    text = 'name="Cool Server"\nport=1\n'
    assert fields(text, ini)["name"].value == "Cool Server"
    out, _ = cf.apply_changes(text, ini, {"name": "Other"})
    assert out == 'name="Other"\nport=1\n'


def test_prose_is_not_mistaken_for_a_settings_file():
    notes = ConfigFile("conf/readme.ini", format="ini")
    assert cf.build_form("This is a long note about the game.\nIt has no settings at all.\nSorry.\n", notes) is None
    assert cf.build_form("", notes) is None
    assert cf.build_form("# only comments\n", notes) is None


def test_plain_text_and_yaml_have_no_form():
    assert cf.build_form("a=1\n", ConfigFile("notes.txt", format="text")) is None
    assert cf.build_form("a: 1\n", ConfigFile("x.yaml", format="yaml")) is None
    assert cf.build_form("a=1\n", ConfigFile("x.yaml", format="yaml")) is None


# ------------------------------------------------------------------ JSON
JS = ConfigFile("conf/data.json", format="json", sensitive_keys=("token",))
JS_TEXT = """{
  "name": "My Server",
  "slots": 16,
  "ratio": 1.5,
  "pvp": false,
  "token": "abc123",
  "motd": null,
  "groups": [
    {"name": "Admin", "password": "x"},
    {"name": "Guest"}
  ],
  "limits": {"a": 1}
}
"""


def test_json_fields_and_types():
    f = fields(JS_TEXT, JS)
    assert list(f) == ["name", "slots", "ratio", "pvp", "token", "motd", "groups[0].name", "groups[0].password",
                       "groups[1].name", "limits.a"]
    assert (f["name"].type, f["name"].value) == ("text", "My Server")
    assert (f["slots"].type, f["slots"].value) == ("number", "16")
    assert (f["ratio"].type, f["ratio"].value) == ("number", "1.5")
    assert (f["pvp"].type, f["pvp"].value) == ("bool", "false")
    assert f["token"].sensitive and not f["name"].sensitive
    assert f["motd"].type == "json" and f["motd"].value == "null"
    assert f["groups[0].name"].value == "Admin" and f["groups[0].name"].label == "groups[0].name"   # nested: dotted label
    assert f["groups[0].name"].group == "groups[0]" and f["limits.a"].group == "limits"
    assert f["limits.a"].type == "number" and f["name"].label == "Name" and f["name"].group == ""


def test_json_edit_is_minimal_and_valid():
    out, changed = cf.apply_changes(JS_TEXT, JS, {
        "name": 'The "best" one', "slots": "8", "pvp": "true", "groups[1].name": "Only", "motd": '"hello"'})
    assert sorted(changed) == ["groups[1].name", "motd", "name", "pvp", "slots"]
    data = json.loads(out)
    assert data["name"] == 'The "best" one' and data["slots"] == 8 and data["pvp"] is True
    assert data["groups"] == [{"name": "Admin", "password": "x"}, {"name": "Only"}] and data["motd"] == "hello"
    assert data["ratio"] == 1.5 and data["limits"] == {"a": 1} and data["token"] == "abc123"
    assert '"ratio": 1.5,\n  "pvp": true,' in out  # formatting around the edits is untouched


def test_json_round_trip_is_exact():
    f = fields(JS_TEXT, JS)
    out, changed = cf.apply_changes(JS_TEXT, JS, {k: v.value for k, v in f.items()})
    assert out == JS_TEXT and changed == []


def test_json_validation():
    with pytest.raises(ValidationError, match="number"):
        cf.apply_changes(JS_TEXT, JS, {"slots": "many"})
    with pytest.raises(ValidationError, match="number"):
        cf.apply_changes(JS_TEXT, JS, {"slots": "true"})
    with pytest.raises(ValidationError, match="true or false"):
        cf.apply_changes(JS_TEXT, JS, {"pvp": "yes"})
    with pytest.raises(ValidationError, match="not valid JSON"):
        cf.apply_changes(JS_TEXT, JS, {"motd": "[{"})


@pytest.mark.parametrize("text", ["[1, 2]", '"x"', "{", '{"a": 1,}', '{"a": 1} trailing', "// comment\n{}", ""])
def test_json_that_is_not_a_flat_object_has_no_form(text):
    assert cf.build_form(text, JS) is None


def test_empty_json_object_has_an_empty_form():
    f = cf.build_form("{}", JS)
    assert f is not None and f.fields == ()


def test_json_masked_secret_is_kept():
    masked = JS_TEXT.replace('"abc123"', f'"{MASK}"')
    out, changed = cf.apply_changes(masked, JS, {"token": MASK, "slots": "2"})
    assert changed == ["slots"] and f'"token": "{MASK}"' in out


def test_apply_without_a_form_is_refused():
    with pytest.raises(ValidationError, match="no form view"):
        cf.apply_changes("just text", ConfigFile("a.txt"), {"a": "1"})


# ------------------------------------------------------------------ service and API (safety rules still apply)
INI = "conf/settings.ini"
HOT = "conf/hot.txt"
JSON_PATH = "conf/data.json"


def write(tmp_path, rel, text):
    p = tmp_path / "data" / "one" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_reading_a_file_includes_its_form_with_secrets_masked(service, deployed, tmp_path):
    write(tmp_path, INI, "name=Alpha\nAdminPassword=s3cret\nslots=8\n")
    r = service.read_config_file("one", INI)
    assert r.form is not None and r.form.kind == "keyvalue"
    by = {f.id: f for f in r.form.fields}
    assert by["AdminPassword"].value == MASK and by["AdminPassword"].sensitive
    assert "s3cret" not in repr(r)


def test_file_without_a_form_still_reads(service, deployed, tmp_path):
    write(tmp_path, HOT, "just some text\n")
    r = service.read_config_file("one", HOT)
    assert r.form is None and r.content == "just some text\n"


def test_form_save_goes_through_the_normal_write_path(service, deployed, tmp_path):
    p = write(tmp_path, INI, "name=Alpha\nAdminPassword=s3cret\nslots=8\n")
    res = service.config_form("one", INI, changes={"slots": "12", "AdminPassword": MASK})
    assert res.changed == ("slots",) and f"AdminPassword={MASK}" in res.content
    saved = service.write_config_file("one", INI, res.content)
    assert saved.backup and (p.parent / saved.backup).read_text() == "name=Alpha\nAdminPassword=s3cret\nslots=8\n"
    assert p.read_text() == "name=Alpha\nAdminPassword=s3cret\nslots=12\n"  # the real secret was restored, not the mask


def test_form_can_change_a_secret(service, deployed, tmp_path):
    p = write(tmp_path, INI, "name=Alpha\nAdminPassword=s3cret\n")
    res = service.config_form("one", INI, changes={"AdminPassword": "n3w"})
    assert "n3w" in res.content  # the new value is the user's own input
    service.write_config_file("one", INI, res.content)
    assert p.read_text() == "name=Alpha\nAdminPassword=n3w\n"


def test_form_view_of_unsaved_raw_text(service, deployed, tmp_path):
    write(tmp_path, INI, "name=Alpha\nslots=8\n")
    res = service.config_form("one", INI, content="name=Beta\nslots=9\nextra=1\n")
    assert [f.id for f in res.form.fields] == ["name", "slots", "extra"] and res.changed == ()
    assert res.content == "name=Beta\nslots=9\nextra=1\n"  # nothing was written
    assert (tmp_path / "data" / "one" / INI).read_text() == "name=Alpha\nslots=8\n"


def test_running_server_still_cannot_save_a_file_that_is_not_hot_editable(service, deployed, tmp_path):
    write(tmp_path, INI, "slots=8\n")
    service.start("one")
    res = service.config_form("one", INI, changes={"slots": "9"})  # previewing is harmless
    from gamedock.errors import ConflictError
    with pytest.raises(ConflictError, match="Stop the server"):
        service.write_config_file("one", INI, res.content)
    assert (tmp_path / "data" / "one" / INI).read_text() == "slots=8\n"
    assert service.read_config_file("one", INI).editable_now is False


def test_traversal_and_undeclared_files_are_refused_by_the_form_api(service, deployed):
    with pytest.raises(Exception):
        service.config_form("one", "../../etc/passwd", content="a=1")
    with pytest.raises(Exception):
        service.config_form("one", "conf/other.ini", content="a=1")


def test_symlinked_config_is_refused(service, deployed, tmp_path):
    target = tmp_path / "outside.ini"
    target.write_text("a=1\n")
    p = tmp_path / "data" / "one" / INI
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(target)
    with pytest.raises(ValidationError, match="symlink"):
        service.config_form("one", INI)
    with pytest.raises(ValidationError, match="symlink"):
        service.read_config_file("one", INI)


def test_missing_file_is_explained_plainly(service, deployed):
    r = service.read_config_file("one", INI)
    assert not r.info.exists and r.form is None
    assert r.note.startswith("This game has not created its settings file yet; it appears after the first full start")
    with pytest.raises(ValidationError, match="has not created its settings file yet"):
        service.config_form("one", INI)


def test_json_form_in_the_service(service, deployed, tmp_path):
    write(tmp_path, JSON_PATH, '{"name": "x", "token": "tok", "slots": 4}\n')
    res = service.config_form("one", JSON_PATH, changes={"slots": "5"})
    service.write_config_file("one", JSON_PATH, res.content)
    assert json.loads((tmp_path / "data" / "one" / JSON_PATH).read_text()) == {"name": "x", "token": "tok", "slots": 5}


def test_endpoints(service, deployed, tmp_path):
    write(tmp_path, INI, "name=Alpha\nAdminPassword=s3cret\nslots=8\n")
    c = TestClient(create_app(service))
    r = c.get("/api/servers/one/config/file", params={"path": INI}).json()
    assert r["form"]["kind"] == "keyvalue" and "s3cret" not in json.dumps(r)
    assert [f["id"] for f in r["form"]["fields"]] == ["name", "AdminPassword", "slots"]
    assert r["form"]["fields"][2] == {"id": "slots", "key": "slots", "label": "Slots", "value": "8", "type": "number",
                                      "sensitive": False, "group": ""}
    p = c.post("/api/servers/one/config/form", json={"path": INI, "changes": {"slots": "9"}}).json()
    assert p["changed"] == ["slots"] and "slots=9" in p["content"] and "s3cret" not in json.dumps(p)
    q = c.post("/api/servers/one/config/form", json={"path": INI, "content": "a=1\n"}).json()
    assert q["form"]["fields"][0]["id"] == "a"
    bad = c.post("/api/servers/one/config/form", json={"path": INI, "changes": {"nope": "1"}})
    assert bad.status_code == 400 and "Unknown setting" in bad.json()["error"]
    assert c.post("/api/servers/one/config/form", json={"path": "../../x"}).status_code in (400, 404)
    assert c.post("/api/servers/nope/config/form", json={"path": INI}).status_code == 404
    assert (tmp_path / "data" / "one" / INI).read_text().endswith("slots=8\n")  # previewing never writes


def test_mcp_read_config_file_stays_text_only(service, deployed, tmp_path):
    from .mcp_helpers import call, make_app, run
    write(tmp_path, INI, "name=Alpha\nslots=8\n")

    async def go(c):
        return await call(c, "read_config_file", server_id="one", path=INI)

    err, r = run(make_app(service), go)
    assert not err and "form" not in r and r["content"].startswith("name=Alpha")
