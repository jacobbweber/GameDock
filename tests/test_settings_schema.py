"""0.6.3: allowed setting values (catalog schemas), enforced in the API and every MCP write tool, and the Enshrouded
tombstoneMode "Off" incident end to end (refused, shown as invalid, and explained as "Failed to start")."""
import json
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from gamedock import configforms as cf
from gamedock import problems as pb
from gamedock import settings_schema as ss
from gamedock.backends import FakeBackend
from gamedock.catalog import load_catalog, parse_definition
from gamedock.errors import CatalogError
from gamedock.models import ConfigFile
from gamedock.service import GameDockService
from gamedock.web.app import create_app

from .mcp_helpers import call, make_app, run
from .test_enshrouded import REAL_JSON, SETTINGS_PATH

CATALOG = Path(__file__).resolve().parent.parent / "catalog"
OFF_JSON = REAL_JSON.replace('"tombstoneMode": "AddBackpackMaterials"', '"tombstoneMode": "Off"')
INCIDENT_LOG = (
    "[Session] 'HostOnline' (up)!\n"
    "[error] Failed to parse enum 'TombstoneMode: Invalid value 'Off'. Supported 'TombstoneMode' values: "
    "AddBackpackMaterials, Everything, NoTombstone\n"
    "[error] Failed to parse Config!\n"
    "INFO gave up: enshrouded entered FATAL state, too many start retries too quickly\n"
)


@pytest.fixture
def ens(tmp_path):
    (tmp_path / "cat").mkdir()
    shutil.copy(CATALOG / "enshrouded.yaml", tmp_path / "cat" / "enshrouded.yaml")
    backend = FakeBackend()
    svc = GameDockService(backend, tmp_path / "cat", tmp_path / "data")
    sid = svc.deploy("enshrouded", "Ens").id
    p = tmp_path / "data" / sid / SETTINGS_PATH
    p.parent.mkdir(parents=True)
    p.write_text(REAL_JSON)
    return svc, backend, sid, p


def test_incident_file_really_has_the_valid_value():
    assert '"tombstoneMode": "AddBackpackMaterials"' in REAL_JSON and OFF_JSON != REAL_JSON


# ------------------------------------------------------------------ schema parsing
def test_parse_and_check_values():
    sp = ss.parse_spec("m", {"choices": ["A", {"value": "B", "label": "Bee", "aliases": ["b"]}]}, "t")
    assert sp.type == "enum" and ss.check_value(sp, "A") is None and ss.check_value(sp, "b") is None
    assert "A" in ss.check_value(sp, "Off") and "B" in ss.check_value(sp, "Off")
    n = ss.parse_spec("n", {"type": "integer", "min": 1, "max": 16}, "t")
    assert ss.check_value(n, "8") is None and ss.check_value(n, "0") and ss.check_value(n, "17") and ss.check_value(n, "x")
    assert ss.check_value(ss.parse_spec("b", {"type": "boolean"}, "t"), "maybe")


@pytest.mark.parametrize("bad", [{"type": "colour"}, {"choices": []}, {"type": "integer", "min": 5, "max": 1},
                                 {"choices": ["Normal", False]}])
def test_malformed_rules_are_refused(bad):
    with pytest.raises(CatalogError):
        ss.parse_spec("x", bad, "t")


def test_unquoted_yaml_off_is_caught():
    y = ("id: g\nname: G\nimage: busybox:1\nconfig_files:\n  - path: c.ini\n    format: ini\n"
         "    schema:\n      mode: { choices: [normal, off] }\n")
    with pytest.raises(CatalogError):
        parse_definition(yaml.safe_load(y))


def test_shipped_catalog_schemas():
    cat = load_catalog(CATALOG)
    ens = {sp.path: sp for sp in cat["enshrouded"].config_files[0].schema}
    assert [c.value for c in ens["gameSettings.tombstoneMode"].choices] == ["AddBackpackMaterials", "Everything", "NoTombstone"]
    assert (ens["slotCount"].min, ens["slotCount"].max) == (1, 16)
    assert any(c.schema for c in cat["palworld"].config_files) and any(c.schema for c in cat["minecraft-java"].config_files)


# ------------------------------------------------------------------ the incident
def test_validate_text_finds_off():
    c = load_catalog(CATALOG)["enshrouded"].config_files[0]
    bad = cf.validate_text(OFF_JSON, c)
    assert len(bad) == 1 and bad[0]["value"] == "Off" and "NoTombstone" in json.dumps(bad[0])
    assert cf.validate_text(REAL_JSON, c) == []


def test_service_refuses_off_and_force_saves(ens):
    svc, _, sid, p = ens
    with pytest.raises(ss.SettingsInvalid) as e:
        svc.write_config_file(sid, SETTINGS_PATH, OFF_JSON)
    assert "tombstoneMode" in json.dumps(e.value.problems) and '"AddBackpackMaterials"' in p.read_text()
    svc.write_config_file(sid, SETTINGS_PATH, OFF_JSON, force=True)
    assert '"Off"' in p.read_text()
    r = svc.read_config_file(sid, SETTINGS_PATH)
    assert r.invalid and "Off" in json.dumps(r.invalid)
    f = {x.id: x for x in r.form.fields}["gameSettings.tombstoneMode"]
    assert f.invalid and f.setting["choices"]


def test_web_api_400_then_save_anyway(ens):
    svc, _, sid, p = ens
    c = TestClient(create_app(svc))
    url = f"/api/servers/{sid}/config/file"
    r = c.put(url, json={"path": SETTINGS_PATH, "content": OFF_JSON})
    assert r.status_code == 400 and r.json()["invalid_settings"]
    assert c.put(url, json={"path": SETTINGS_PATH, "content": OFF_JSON, "force": True}).status_code == 200
    assert '"Off"' in p.read_text()


def test_mcp_refuses_without_override(ens):
    svc, _, sid, p = ens
    app = make_app(svc, "full")

    async def go(c):
        a = await call(c, "write_config_file", server_id=sid, path=SETTINGS_PATH, content=OFF_JSON)
        b = await call(c, "update_config_settings", server_id=sid, path=SETTINGS_PATH,
                       changes={"gameSettings.tombstoneMode": "Off"})
        o = await call(c, "get_setting_options", server_id=sid, path=SETTINGS_PATH, setting="tombstoneMode")
        ok = await call(c, "update_config_settings", server_id=sid, path=SETTINGS_PATH,
                        changes={"gameSettings.tombstoneMode": "NoTombstone"})
        return a, b, o, ok
    a, b, o, ok = run(app, go)
    assert a[0] and b[0] and "invalid_settings" in a[1] and "AddBackpackMaterials" in b[1]
    assert not o[0] and [c["value"] for c in o[1]["config_files"][0]["settings"][0]["choices"]] == \
        ["AddBackpackMaterials", "Everything", "NoTombstone"]
    assert not ok[0] and '"NoTombstone"' in p.read_text()


def test_env_choices_enforced_on_deploy_and_update(tmp_path):
    (tmp_path / "cat").mkdir()
    shutil.copy(CATALOG / "enshrouded.yaml", tmp_path / "cat" / "enshrouded.yaml")
    svc = GameDockService(FakeBackend(), tmp_path / "cat", tmp_path / "data")
    with pytest.raises(ss.SettingsInvalid):
        svc.deploy("enshrouded", "E", env={"SERVER_SLOT_COUNT": "40"})
    sid = svc.deploy("enshrouded", "E", env={"SERVER_SLOT_COUNT": "8"}).id
    with pytest.raises(ss.SettingsInvalid):
        svc.update(sid, env={"SERVER_SLOT_COUNT": "0"})


def test_incident_log_becomes_a_failed_card(ens):
    svc, backend, sid, p = ens
    p.write_text(OFF_JSON)
    svc.start(sid)
    backend.set_logs(f"gamedock-{sid}", INCIDENT_LOG)
    svc._problem_cache.clear(); svc._readiness_text.clear()   # the card answer is cached for a few seconds
    s = svc.get_status(sid)
    assert s.phase == "failed" and "Failed to start" in s.phase_detail
    assert s.problem["id"] == "config-invalid" or s.problem.get("setting")
    assert s.problem["value"] == "Off" and "NoTombstone" in json.dumps(s.problem["allowed"])
    assert s.problem.get("config_path") == SETTINGS_PATH


def test_problem_pattern_parses_details():
    pr = pb.scan([("the container log", INCIDENT_LOG)], [])
    assert pr.id == "config-invalid" and pr.fatal and pr.value == "Off" and "TombstoneMode" in pr.setting


def test_verify_has_settings_valid_row(ens):
    svc, _, sid, p = ens
    rows = {r["id"]: r for r in svc.verify_game_setup(sid)["checks"]}
    assert rows["settings-valid"]["status"] == "pass"
    p.write_text(OFF_JSON)
    rows = {r["id"]: r for r in svc.verify_game_setup(sid)["checks"]}
    assert rows["settings-valid"]["status"] == "fail" and "Off" in json.dumps(rows["settings-valid"])


def test_old_snapshot_gets_the_current_schema(ens):
    svc, _, sid, _ = ens
    inst = svc._instance(sid)
    tmpl = svc._template(inst)
    assert svc.config_schema(inst)[0].schema   # comes from the current catalog
    assert tmpl is not None


def test_lint_warns_about_settings_file_without_schema():
    from gamedock import authoring as au
    y = "id: g\nname: G\nimage: busybox:1\nconfig_files:\n  - { path: c.ini, format: ini }\n"
    w = repr(au.analyze_entry(parse_definition(yaml.safe_load(y))))
    assert "schema" in w
