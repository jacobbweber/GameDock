"""Game version tracking for SteamCMD games: the Steam manifest, the newest build, the status, "Update now" and the
scheduled maintenance integration. The fixtures mirror what was seen on a real Palworld server."""
import json
import os
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gamedock import steam
from gamedock.backends import FakeBackend
from gamedock.catalog import parse_definition
from gamedock.config import Settings
from gamedock.errors import CatalogError, ConflictError, ValidationError
from gamedock.models import PortMapping
from gamedock.scheduler import Scheduler
from gamedock.service import GameDockService
from gamedock.web.app import create_app
from tests.mcp_helpers import call, make_app, run

# ----------------------------------------------------------------------------------------- real-life fixtures
APP = "2394010"
MANIFEST = textwrap.dedent('''\
    "AppState"
    {
    \t"appid"\t\t"2394010"
    \t"Universe"\t\t"1"
    \t"name"\t\t"Palworld Dedicated Server"
    \t"StateFlags"\t\t"4"
    \t"installdir"\t\t"."
    \t"LastUpdated"\t\t"1792297341"
    \t"buildid"\t\t"25247047"
    \t"TargetBuildID"\t\t"25247047"
    \t"InstalledDepots"
    \t{
    \t\t"2394012"
    \t\t{
    \t\t\t"manifest"\t\t"7764392431927543301"
    \t\t\t"size"\t\t"4944233334"
    \t\t}
    \t}
    \t"UserConfig"
    \t{
    \t\t"language"\t\t"english"
    \t}
    }
    ''')
INFO_JSON = json.dumps({"data": {APP: {"_change_number": 1, "common": {"name": "Palworld Dedicated Server"},
                                       "depots": {"branches": {"public": {"buildid": "25247047", "timeupdated": "1792297341"},
                                                               "beta": {"buildid": "25300000", "timeupdated": "1792400000"}}}}},
                        "status": "success"})
LOG = ("[INFO] New version available: 2.8.0 Mod Support\n"
       "\x1b[32m[ OK ]\x1b[0m Connecting anonymously to Steam Public...OK\n"
       "[2026.10.18-00:22:11] Game version is v1.0.5.102999\n"
       "[2026.10.18-00:22:12] Running Palworld dedicated server on :8211\n")
PATTERN = r"Game version is (v?[0-9.]+)"


class Mirror:
    """A pretend Steam data service."""

    def __init__(self, build="25247047", updated=1792297341):
        self.build, self.updated = build, updated
        self.calls = []
        self.fail = None          # an exception, or an int status

    def __call__(self, url, timeout):
        self.calls.append(url)
        if isinstance(self.fail, Exception):
            raise self.fail
        if isinstance(self.fail, int):
            return self.fail, "limit"
        doc = json.loads(INFO_JSON)
        doc["data"][APP]["depots"]["branches"]["public"] = {"buildid": self.build, "timeupdated": str(self.updated)}
        return 200, json.dumps(doc)


# ----------------------------------------------------------------------------------------- parsing
def test_parse_manifest_reads_the_palworld_manifest():
    d = steam.parse_manifest(MANIFEST)
    assert d["AppState"]["buildid"] == "25247047" and d["AppState"]["LastUpdated"] == "1792297341"
    assert d["AppState"]["InstalledDepots"]["2394012"]["manifest"] == "7764392431927543301"
    g = steam.installed_from_manifest(d, "x")
    assert (g.build, g.updated_at, g.complete, g.name) == ("25247047", 1792297341.0, True, "Palworld Dedicated Server")


def test_parse_manifest_is_tolerant():
    assert steam.installed_from_manifest(steam.parse_manifest("")) is None
    assert steam.installed_from_manifest(steam.parse_manifest('"AppState" { "buildid" "abc" }')) is None   # not a number
    half = steam.parse_manifest('"AppState"\n{\n\t"buildid"\t\t"77"\n\t"InstalledDepots"\n\t{\n\t\t"1"\n')   # cut off
    assert steam.installed_from_manifest(half).build == "77"
    g = steam.installed_from_manifest(steam.parse_manifest(MANIFEST.replace('"4"', '"1026"', 1)))
    assert g.state_flags == "1026" and not g.complete


def test_render_manifest_round_trips():
    g = steam.installed_from_manifest(steam.parse_manifest(steam.render_manifest("5", "123", 1700000000)))
    assert (g.build, g.updated_at) == ("123", 1700000000.0)


def test_find_manifest(tmp_path):
    vol = tmp_path / "palworld"
    (vol / "steamapps").mkdir(parents=True)
    f = vol / "steamapps" / f"appmanifest_{APP}.acf"
    f.write_text(MANIFEST)
    assert steam.find_manifest(APP, [vol]) == f
    assert steam.find_manifest("1", [vol]) is None
    # deeper, like a game that keeps its files in /config/gamefiles
    deep = tmp_path / "config" / "gamefiles" / "steamapps"
    deep.mkdir(parents=True)
    (deep / "appmanifest_1690800.acf").write_text(MANIFEST)
    assert steam.find_manifest("1690800", [tmp_path / "config"]) == deep / "appmanifest_1690800.acf"
    assert steam.find_manifest("1690800", [tmp_path / "config"], max_depth=0) is None       # bounded depth
    assert steam.find_manifest("1690800", [tmp_path / "config"], max_entries=1) is None     # bounded work


def test_find_manifest_with_explicit_folder_and_symlinks(tmp_path):
    base = tmp_path / "inst"
    (base / "game" / "steamapps").mkdir(parents=True)
    f = base / "game" / "steamapps" / f"appmanifest_{APP}.acf"
    f.write_text(MANIFEST)
    assert steam.find_manifest(APP, [], "game", base) == f              # the folder above steamapps
    assert steam.find_manifest(APP, [], "game/steamapps", base) == f    # or steamapps itself
    assert steam.find_manifest(APP, [], "nope", base) is None
    outside = tmp_path / "outside"
    (outside / "steamapps").mkdir(parents=True)
    (outside / "steamapps" / f"appmanifest_{APP}.acf").write_text(MANIFEST)
    os.symlink(outside, base / "link")
    assert steam.find_manifest(APP, [], "link", base) is None           # may not leave the data folder
    vol = tmp_path / "vol"
    vol.mkdir()
    os.symlink(outside, vol / "sneaky")
    assert steam.find_manifest(APP, [vol]) is None                      # symlinked folders are not followed


def test_parse_steam_info():
    assert steam.parse_steam_info(INFO_JSON, APP, "public") == ("25247047", 1792297341.0)
    assert steam.parse_steam_info(INFO_JSON, APP, "beta")[0] == "25300000"
    for text, why in (("<html>", "not JSON"), ("{}", "no information"), (INFO_JSON, "no 'nope' branch")):
        branch = "nope" if why.startswith("no 'nope'") else "public"
        app = "1" if why == "no information" else APP
        with pytest.raises(steam.SteamInfoError, match=why.split(" ", 1)[1] if " " in why else why):
            steam.parse_steam_info(text, app, branch)


def test_extract_version_picks_the_game_line_not_the_image_line():
    assert steam.extract_version(LOG, PATTERN) == "v1.0.5.102999"
    assert steam.extract_version(LOG + "Game version is v1.0.6.1\n", PATTERN) == "v1.0.6.1"      # the newest line wins
    assert steam.extract_version("New version available: 2.8.0 Mod Support", PATTERN) == ""
    assert steam.extract_version(LOG, "") == "" and steam.extract_version(LOG, "(") == ""


# ----------------------------------------------------------------------------------------- the newest build
class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def info(tmp_path, mirror, clock=None, **kw):
    return steam.SteamInfo(tmp_path / "steam-latest.json", http_get=mirror, clock=clock or Clock(), **kw)


def test_latest_build_is_cached_for_an_hour_and_persisted(tmp_path):
    mirror, clock = Mirror(), Clock()
    s = info(tmp_path, mirror, clock)
    a = s.get(APP, "public")
    assert a.ok and a.build == "25247047" and a.updated_at == 1792297341.0 and len(mirror.calls) == 1
    assert mirror.calls[0] == "https://api.steamcmd.net/v1/info/2394010"
    clock.t += 3599
    s.get(APP, "public")
    assert len(mirror.calls) == 1                                       # still fresh
    assert not s.due(APP, "public")
    clock.t += 2
    assert s.due(APP, "public")
    s.get(APP, "public")
    assert len(mirror.calls) == 2
    # a restart of GameDock (a new object, the same file) does not ask again
    s2 = info(tmp_path, mirror, clock)
    assert s2.cached(APP, "public").build == "25247047" and not s2.due(APP, "public")
    s2.get(APP, "public")
    assert len(mirror.calls) == 2


def test_branch_and_force(tmp_path):
    mirror, clock = Mirror(), Clock()
    s = info(tmp_path, mirror, clock)
    assert s.get(APP, "beta").build == "25300000"        # each branch has its own entry in the answer and in the cache
    mirror.build = "25999999"
    s.get(APP, "public")
    assert s.get(APP, "public").build == "25999999"
    calls = len(mirror.calls)
    s.get(APP, "public", force=True)
    assert len(mirror.calls) == calls                    # pressed twice within the gap: same answer
    clock.t += 25
    mirror.build = "26000000"
    assert s.get(APP, "public", force=True).build == "26000000"


@pytest.mark.parametrize("fail,words", [(OSError("no route"), "Could not reach"), (429, "limiting"), (500, "unexpected status")])
def test_failures_are_quiet(tmp_path, fail, words):
    mirror, clock = Mirror(), Clock()
    mirror.fail = fail
    s = info(tmp_path, mirror, clock)
    r = s.get(APP, "public")                              # never raises
    assert not r.ok and words in r.error and r.build == ""
    s.get(APP, "public")
    assert len(mirror.calls) == 1                         # a failure is not retried at once
    clock.t += 601
    mirror.fail = None
    assert s.get(APP, "public").ok and len(mirror.calls) == 2


def test_garbage_answer_is_a_could_not_check(tmp_path):
    s = info(tmp_path, lambda url, t: (200, "<html>nope</html>"))
    r = s.get(APP, "public")
    assert not r.ok and "not JSON" in r.error


def test_switched_off_never_contacts_anything(tmp_path):
    mirror = Mirror()
    s = steam.SteamInfo(tmp_path / "c.json", http_get=mirror, enabled=False)
    r = s.get(APP, "public", force=True)
    assert not r.ok and "switched off" in r.error and not mirror.calls and not s.due(APP, "public")


def test_damaged_cache_file_is_ignored(tmp_path):
    (tmp_path / "steam-latest.json").write_text("{nonsense")
    assert info(tmp_path, Mirror()).cached(APP, "public") is None


def test_url_template(monkeypatch):
    assert steam.SteamInfo.check_template("https://mirror.example/info/{appid}.json") == "https://mirror.example/info/{appid}.json"
    assert steam.SteamInfo.check_template("https://mirror.example/info") == "https://mirror.example/info/{appid}"
    with pytest.raises(ValueError):
        steam.SteamInfo.check_template("ftp://x/{appid}")
    monkeypatch.setenv("GAMEDOCK_STEAM_INFO_URL", "https://mirror.example/v2/{appid}")
    assert Settings.from_env().steam_info_url == "https://mirror.example/v2/{appid}"
    seen = []
    s = steam.SteamInfo(url_template="https://mirror.example/v2/{appid}", http_get=lambda u, t: seen.append(u) or (200, INFO_JSON))
    s.get(APP)
    assert seen == ["https://mirror.example/v2/2394010"]


def test_compute_status():
    inst = steam.InstalledGame("100", 1.0)
    new = steam.LatestBuild(APP, "public", True, "101", 2.0, 5.0)
    same = steam.LatestBuild(APP, "public", True, "100", 2.0, 5.0)
    bad = steam.LatestBuild(APP, "public", False, error="Could not reach the Steam data service")
    assert steam.compute_status(True, inst, new)[0] == steam.UPDATE_AVAILABLE
    assert steam.compute_status(True, inst, same)[0] == steam.UP_TO_DATE
    assert steam.compute_status(True, steam.InstalledGame("200", 1.0), new)[0] == steam.UP_TO_DATE    # a beta, ahead of public
    s, text = steam.compute_status(True, inst, bad)
    assert s == steam.COULD_NOT_CHECK and "Could not reach" in text
    assert steam.compute_status(True, inst, None)[0] == steam.COULD_NOT_CHECK
    s, text = steam.compute_status(True, None, same, "no manifest here")
    assert s == steam.COULD_NOT_CHECK and text == "no manifest here"
    assert steam.compute_status(False, None, None)[0] == steam.UNKNOWN


# ----------------------------------------------------------------------------------------- catalog
BASE = "id: g\nname: G\nimage: g:1\n"


def test_catalog_steam_fields_and_defaults():
    e = parse_definition(__import__("yaml").safe_load(BASE + "steam_app_id: 2394010\nversion_log_pattern: 'Game version is (v?[0-9.]+)'\n"))
    assert (e.steam_app_id, e.steam_branch, e.game_update_method) == ("2394010", "", "restart")
    assert parse_definition(__import__("yaml").safe_load(BASE)).game_update_method == "none"
    e2 = parse_definition(__import__("yaml").safe_load(BASE + "steam_app_id: '2394010'\nsteam_branch: beta\nsteam_manifest_dir: game\ngame_update_method: none\n"))
    assert (e2.steam_branch, e2.steam_manifest_dir, e2.game_update_method) == ("beta", "game", "none")


@pytest.mark.parametrize("extra,words", [
    ("steam_app_id: abc", "steam_app_id must be a number"),
    ("steam_app_id: 1\nsteam_branch: 'a b'", "steam_branch"),
    ("steam_app_id: 1\nsteam_manifest_dir: ../x", "steam_manifest_dir"),
    ("steam_app_id: 1\nsteam_manifest_dir: /abs", "steam_manifest_dir"),
    ("steam_app_id: 1\ngame_update_method: magic", "game_update_method"),
    ("game_update_method: restart", "needs steam_app_id"),
    ("steam_branch: beta", "only make sense together"),
    ("steam_app_id: 1\nversion_log_pattern: 'a(b'", "version_log_pattern"),
    ("steam_app_id: 1\nversion_log_pattern: 'Game version is v[0-9.]+'", "exactly 1 capture group"),
])
def test_catalog_steam_validation(extra, words):
    with pytest.raises(CatalogError, match=words):
        parse_definition(__import__("yaml").safe_load(BASE + extra + "\n"))


def test_shipped_catalog_entries_declare_steam_ids():
    from pathlib import Path

    from gamedock.catalog import load_catalog
    c = load_catalog(Path(__file__).resolve().parent.parent / "catalog")
    assert {k: v.steam_app_id for k, v in c.items() if v.steam_app_id} == {
        "palworld": "2394010", "enshrouded": "2278520", "valheim": "896660", "satisfactory": "1690800"}
    assert not c["minecraft-java"].steam_app_id
    pal = c["palworld"]
    assert pal.env["UPDATE_ON_BOOT"] == "true" and pal.game_update_method == "restart"
    assert steam.extract_version(LOG, pal.version_log_pattern) == "v1.0.5.102999"


# ----------------------------------------------------------------------------------------- the service
STEAMY = textwrap.dedent("""\
    id: steamy
    name: Steamy Game
    image: steamy:1
    steam_app_id: 2394010
    version_log_pattern: 'Game version is (v?[0-9.]+)'
    ready_log_pattern: 'Running Palworld dedicated server'
    ports:
      - { protocol: udp, host: 8211, container: 8211 }
    volumes:
      - { host: ./palworld, container: /palworld }
    fields:
      - { name: ADMIN_PASSWORD, secret: true, required: true }
    """)
PLAIN = "id: plain\nname: Plain\nimage: plain:1\nvolumes:\n  - { host: ./data, container: /data }\n"


class DClock:
    def __init__(self):
        self.t = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class World:
    """A game server whose files update to `available` whenever its container starts (like UPDATE_ON_BOOT)."""

    def __init__(self, tmp_path):
        self.mirror = Mirror()
        self.clock = DClock()
        self.backend = FakeBackend()
        cat = tmp_path / "catalog"
        cat.mkdir()
        (cat / "steamy.yaml").write_text(STEAMY)
        (cat / "plain.yaml").write_text(PLAIN)
        self.steam = steam.SteamInfo(tmp_path / "data" / "steam-latest.json", http_get=self.mirror, manual_gap=0)
        self.service = GameDockService(self.backend, cat, tmp_path / "data", clock=self.clock, steam_info=self.steam,
                                       game_update_wait=5, game_update_poll=0, game_update_settle=0)
        self.service._sleep = lambda s: None
        self.installed = "25247047"      # the build in the server's files
        self.updates_on_start = True     # False: the image does NOT update the game when it starts
        self.sid = self.service.deploy("steamy", "Pal", env={"ADMIN_PASSWORD": "pw"}).id
        self.service._manifest_missing.clear()      # (deploying looked for the manifest; the real search repeats after 30 s)
        self.backend.simulate_steam_game("gamedock-" + self.sid, APP, self._build,
                                         version=lambda b: "Game version is v1.0." + b[-4:])
        (self.service.data_dir / self.sid / "palworld").mkdir(parents=True, exist_ok=True)
        (self.service.data_dir / self.sid / "palworld" / "world.sav").write_text("world")

    def _build(self):
        if self.updates_on_start:
            self.installed = self.mirror.build
        return self.installed, 1792297341

    def release(self, build="25301234"):
        self.mirror.build = build
        self.mirror.updated = 1792400000
        self.steam._entries.clear()          # (tests only) forget the cached answer; real code waits an hour or Check now

    def start_ready(self):
        self.service.start(self.sid)
        self.service._manifest_missing.clear()      # (the real search is repeated after 30 seconds)
        self.backend.append_log("gamedock-" + self.sid, "Running Palworld dedicated server on :8211")


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_server_without_steam_id_is_unknown(world):
    sid = world.service.deploy("plain", "Plain").id
    s = world.service.get_status(sid)
    assert s.game_update_status == "unknown" and s.steam_app_id == "" and s.game_version == ""
    r = world.service.get_game_update_status(sid)
    assert r["supported"] is False and r["status_label"] == "Unknown (no Steam id for this game)" and not r["can_update"]
    with pytest.raises(ValidationError, match="cannot update the game files"):
        world.service.update_game(sid, confirm=True)
    assert world.service.check_game_update(sid)["status"] == "unknown" and not world.mirror.calls


def test_status_up_to_date_and_update_available(world):
    w, svc = world, world.service
    w.start_ready()
    s = svc.get_status(w.sid)
    assert s.installed_build == "25247047" and s.latest_build == "" and s.game_update_status == "could_not_check"
    assert not w.mirror.calls                                    # showing the tile never uses the network
    r = svc.check_game_update(w.sid)
    assert r["status"] == "up_to_date" and r["status_label"] == "Up to date" and r["update_available"] is False
    assert r["installed"]["build"] == "25247047" and r["installed"]["found"] and r["installed"]["complete"]
    assert r["installed"]["updated_at"] == datetime.fromtimestamp(1792297341, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert r["latest"]["build"] == "25247047" and r["game_version"] == "v1.0.7047" and r["can_update"]
    s = svc.get_status(w.sid)
    assert (s.game_version, s.installed_build, s.latest_build, s.game_update_status) == (
        "v1.0.7047", "25247047", "25247047", "up_to_date")
    w.release()
    r = svc.check_game_update(w.sid)
    assert r["status"] == "update_available" and r["update_available"] is True
    assert r["latest"]["build"] == "25301234" and "25247047" in r["summary"] and "25301234" in r["summary"]
    assert svc.get_status(w.sid).game_update_status == "update_available"
    calls = len(w.mirror.calls)
    for _ in range(5):
        svc.list_servers()
    assert len(w.mirror.calls) == calls


def test_could_not_check_when_offline_or_manifest_missing(world, tmp_path):
    w, svc = world, world.service
    w.start_ready()
    w.mirror.fail = OSError("down")
    r = svc.check_game_update(w.sid)
    assert r["status"] == "could_not_check" and "Could not reach" in r["summary"] and r["update_available"] is None
    assert r["latest"]["error"] and r["installed"]["build"] == "25247047" and r["game_version"]
    # a server whose game files are not in any volume: the installed build cannot be read
    import shutil
    shutil.rmtree(svc.data_dir / w.sid / "palworld" / "steamapps")
    svc._manifest_cache.clear(); svc._manifest_missing.clear()
    w.mirror.fail = None
    w.steam._entries.clear()
    r = svc.check_game_update(w.sid)
    assert r["status"] == "could_not_check" and not r["installed"]["found"] and "appmanifest_2394010.acf" in r["summary"]


def test_update_checks_switched_off(world):
    w = world
    w.service.steam.enabled = False
    w.start_ready()
    r = w.service.check_game_update(w.sid)
    assert r["status"] == "could_not_check" and "switched off" in r["summary"] and not w.mirror.calls
    assert r["checks_enabled"] is False


def test_game_version_survives_log_rotation_and_restart_of_gamedock(world, tmp_path):
    w, svc = world, world.service
    w.start_ready()
    assert svc.get_status(w.sid).game_version == "v1.0.7047"
    w.backend._logs["gamedock-" + w.sid] = ["nothing useful any more"]
    svc._versions.clear()
    svc._version_scan.clear()
    assert svc.get_status(w.sid).game_version == ""                    # logs scrolled and nothing remembered
    w.backend.append_log("gamedock-" + w.sid, "Game version is v1.0.5.102999")
    svc._version_scan.clear()
    assert svc.get_status(w.sid).game_version == "v1.0.5.102999"
    w.backend._logs["gamedock-" + w.sid] = []
    svc2 = GameDockService(w.backend, svc.catalog_dir, svc.data_dir, clock=w.clock, steam_info=w.steam)
    assert svc2.get_status(w.sid).game_version == "v1.0.5.102999"      # remembered on disk, tied to the build id


def test_old_servers_pick_up_steam_settings_from_the_catalog(world):
    w, svc = world, world.service
    inst = svc.store.get(w.sid)
    for k in ("steam_app_id", "steam_branch", "steam_manifest_dir", "version_log_pattern", "game_update_method"):
        inst.template.pop(k, None)              # a server saved by 0.3.0 knows nothing about game versions
    svc.store.put(inst)
    w.start_ready()
    assert svc.get_status(w.sid).installed_build == "25247047"
    assert svc.check_game_update(w.sid)["can_update"]


def test_update_game_flow(world):
    w, svc = world, world.service
    w.start_ready()
    w.release()
    svc.check_game_update(w.sid)
    with pytest.raises(ValidationError, match="confirmation"):
        svc.update_game(w.sid)
    w.backend.calls.clear()
    r = svc.update_game(w.sid, confirm=True)
    assert r["updated"] and r["outcome"] == "changed" and r["restarted"] and not r["problem"]
    assert (r["before_build"], r["after_build"], r["newest_build"]) == ("25247047", "25301234", "25301234")
    assert "from build 25247047 to build 25301234" in r["note"] and r["backup"]
    actions = [a for a, _ in w.backend.calls if a in ("stop", "start")]
    assert actions == ["stop", "start"]                              # backup happened while it was stopped
    assert len(svc.list_backups(w.sid)) == 1 and svc.list_backups(w.sid)[0].kind == "pre-update"
    assert svc.get_status(w.sid).status.value == "running"
    after = svc.check_game_update(w.sid)
    assert after["status"] == "up_to_date" and after["installed"]["build"] == "25301234"


def test_update_game_reports_when_the_build_did_not_change(world):
    w, svc = world, world.service
    w.start_ready()
    w.release()
    w.updates_on_start = False                                      # e.g. UPDATE_ON_BOOT is off in the image
    r = svc.update_game(w.sid, confirm=True)
    assert not r["updated"] and r["outcome"] == "unchanged" and r["problem"] and r["restarted"]
    assert "did not update on restart" in r["note"] and "still on build 25247047" in r["note"] and "25301234" in r["note"]
    assert "check the logs" in r["note"].lower()
    assert svc.get_status(w.sid).status.value == "running"          # the server was left running
    assert svc.get_status(w.sid).game_update_status == "update_available"


def test_update_game_when_already_up_to_date_changes_nothing(world):
    w, svc = world, world.service
    w.start_ready()
    w.backend.calls.clear()
    r = svc.update_game(w.sid, confirm=True)
    assert not r["updated"] and r["outcome"] == "up_to_date" and not r["restarted"] and not svc.list_backups(w.sid)
    assert not [a for a, _ in w.backend.calls if a in ("stop", "start")]


def test_update_game_needs_a_running_server(world):
    w, svc = world, world.service
    w.release()
    with pytest.raises(ValidationError, match="stopped"):
        svc.update_game(w.sid, confirm=True)


def test_update_game_when_build_was_unknown_cannot_verify(world):
    w, svc = world, world.service
    w.start_ready()
    w.release()
    import shutil
    shutil.rmtree(svc.data_dir / w.sid / "palworld" / "steamapps")
    svc._manifest_cache.clear(); svc._manifest_missing.clear()
    r = svc.update_game(w.sid, confirm=True)
    assert r["outcome"] in ("unverified", "changed")                # the restart rewrote the manifest
    assert r["restarted"]


def test_update_game_backup_failure_restarts_and_changes_nothing(world, monkeypatch):
    w, svc = world, world.service
    w.start_ready()
    w.release()
    from gamedock import backups as bk
    monkeypatch.setattr(bk, "create_archive", lambda *a, **k: (_ for _ in ()).throw(ConflictError("disk full")))
    with pytest.raises(ConflictError, match="disk full"):
        svc.update_game(w.sid, confirm=True)
    assert svc.get_status(w.sid).status.value == "running"          # not left down


def test_update_game_activity_is_visible(world):
    w, svc = world, world.service
    w.start_ready()
    w.release()
    seen = []
    orig = svc._wait_for_game_build

    def spy(*a, **k):
        seen.append(svc.get_status(w.sid).activity)
        return orig(*a, **k)

    svc._wait_for_game_build = spy
    svc.update_game(w.sid, confirm=True)
    assert seen == ["updating game"]


def test_background_check_asks_for_old_answers_only(world):
    w, svc = world, world.service
    assert svc.check_game_updates_due() == 1 and len(w.mirror.calls) == 1
    assert svc.check_game_updates_due() == 0 and len(w.mirror.calls) == 1
    sched = Scheduler(svc, tick=999, first_delay=0)
    w.steam._entries.clear()
    sched.run_once()
    assert len(w.mirror.calls) == 2                                  # the scheduler tick refreshes it
    sched2 = Scheduler(svc, background_checks=False)
    w.steam._entries.clear()
    sched2.run_once()
    assert len(w.mirror.calls) == 2


# ----------------------------------------------------------------------------------------- scheduled maintenance
def run_maintenance(w, **opts):
    svc = w.service
    svc.set_maintenance_schedule(w.sid, True, "daily", time_of_day="04:00", **opts)
    r = svc.run_schedule_now(w.sid, "maintenance", confirm=True)
    return r, svc.get_server_schedules(w.sid)["maintenance"]


def test_maintenance_applies_a_game_update_as_part_of_the_restart(world):
    w = world
    w.start_ready()
    w.release()
    r, m = run_maintenance(w, backup_first=True, update_image=True, restart=True)
    assert r["result"] == "ok", r
    msg = r["message"]
    assert "newer game version is available (build 25247047 -> 25301234)" in msg
    assert "updated from build 25247047 to build 25301234" in msg and "restarted" in msg
    assert m["last_message"] == msg                                   # shown in the schedules panel
    assert w.installed == "25301234" and len(w.service.list_backups(w.sid)) == 1


def test_maintenance_without_update_just_restarts(world):
    w = world
    w.start_ready()
    w.backend.calls.clear()
    r, _ = run_maintenance(w, backup_first=False, update_image=True, restart=True)
    assert r["result"] == "ok" and "game is up to date (build 25247047)" in r["message"] and "restarted" in r["message"]
    assert [a for a, _ in w.backend.calls if a in ("stop", "start")] == ["stop", "start"]
    assert not w.service.list_backups(w.sid)


def test_maintenance_update_without_restart_option_still_restarts_to_apply_it(world):
    w = world
    w.start_ready()
    w.release()
    r, _ = run_maintenance(w, backup_first=False, update_image=True, restart=False)
    assert r["result"] == "ok" and w.installed == "25301234" and "updated from build" in r["message"]
    assert len(w.service.list_backups(w.sid)) == 1                    # a safety backup is always taken before updating


def test_maintenance_without_apply_updates_does_not_check_or_update(world):
    w = world
    w.start_ready()
    w.release()
    calls = len(w.mirror.calls)
    r, _ = run_maintenance(w, backup_first=False, update_image=False, restart=True)
    assert len(w.mirror.calls) == calls and "game version" not in r["message"]
    assert w.installed == "25301234"                                  # (this image updates on start anyway; GameDock just did not look)


def test_maintenance_reports_a_game_that_did_not_update(world):
    w = world
    w.start_ready()
    w.release()
    w.updates_on_start = False
    r, m = run_maintenance(w, backup_first=True, update_image=True, restart=True)
    assert r["result"] == "failed" and "did not update on restart" in r["message"] and m["last_result"] == "failed"


def test_maintenance_survives_a_check_that_failed(world):
    w = world
    w.start_ready()
    w.mirror.fail = OSError("down")
    r, _ = run_maintenance(w, backup_first=False, update_image=True, restart=True)
    assert r["result"] == "ok" and "Could not check for a game update" in r["message"] and "restarted" in r["message"]


def test_maintenance_on_a_stopped_server_leaves_it_stopped(world):
    w = world
    w.start_ready()
    w.service.stop(w.sid)
    w.release()
    r, _ = run_maintenance(w, backup_first=False, update_image=True, restart=True)
    assert "stopped; the game will update the next time it starts" in r["message"] and "left stopped" in r["message"]
    assert w.service.get_status(w.sid).status.value == "stopped"


def test_apply_updates_is_allowed_for_games_that_update_in_the_game_only(world):
    w, svc = world, world.service
    v = svc.get_server_schedules(w.sid)["maintenance"]
    assert v["game_update_supported"] and v["image_update_supported"]
    plain = svc.deploy("plain", "Plain").id
    assert not svc.get_server_schedules(plain)["maintenance"]["game_update_supported"]


# ----------------------------------------------------------------------------------------- web and MCP
def test_web_endpoints(world):
    w = world
    w.start_ready()
    w.release()
    c = TestClient(create_app(w.service))
    r = c.get(f"/api/servers/{w.sid}/game-update").json()
    assert r["status"] == "could_not_check"                           # nothing asked yet
    r = c.post(f"/api/servers/{w.sid}/game-update/check").json()
    assert r["status"] == "update_available" and r["latest"]["build"] == "25301234"
    s = c.get(f"/api/servers/{w.sid}").json()
    assert s["game_update_status"] == "update_available" and s["installed_build"] == "25247047" and s["latest_build"] == "25301234"
    assert s["game_version"].startswith("v1.0.")
    assert c.post(f"/api/servers/{w.sid}/update-game").status_code == 400            # needs confirm=true
    r = c.post(f"/api/servers/{w.sid}/update-game?confirm=true").json()
    assert r["outcome"] == "changed" and r["after_build"] == "25301234"
    assert c.get("/api/servers/nope/game-update").status_code == 404
    cat = {e["id"]: e for e in c.get("/api/catalog").json()}
    assert cat["steamy"]["steam_app_id"] == "2394010" and cat["steamy"]["steam_branch"] == "public"


def test_mcp_tools(world):
    w = world
    w.start_ready()
    w.release()
    svc = w.service

    async def check(c):
        r = await call(c, "check_game_update", server_id=w.sid)
        u = await call(c, "update_game", server_id=w.sid)
        return r, u

    r, u = run(make_app(svc, "read_only"), check)
    assert not r[0] and r[1]["status"] == "update_available" and r[1]["installed"]["build"] == "25247047"
    assert u[0] and "Unknown tool" in u[1]                            # read-only mode cannot update
    r, u = run(make_app(svc, "standard"), check)
    assert not u[0] and u[1]["outcome"] == "changed" and u[1]["after_build"] == "25301234"
    assert w.installed == "25301234"

    async def guide(c):
        return (await c.read_resource("gamedock://guide")).contents[0].text

    text = run(make_app(svc, "standard"), guide)
    assert "check_game_update" in text and "update_game" in text and "game_update_status" in text


def test_mcp_check_is_audited(world):
    w = world
    app = make_app(w.service, "read_only")
    run(app, lambda c: call(c, "check_game_update", server_id=w.sid))
    rows = [json.loads(x) for x in (w.service.data_dir / "mcp-audit.jsonl").read_text().splitlines()]
    assert rows[-1]["tool"] == "check_game_update" and rows[-1]["status"] == "ok"
