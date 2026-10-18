"""Is the game joinable? Pure helpers, catalog validation, the service's phase logic and the log endpoints."""
import textwrap
import threading
import time

import pytest
from fastapi.testclient import TestClient

from gamedock import readiness as rd
from gamedock import service as service_module
from gamedock.catalog import parse_definition
from gamedock.errors import CatalogError, ValidationError
from gamedock.models import Status
from gamedock.service import GameDockService
from gamedock.web.app import create_app

from .mcp_helpers import call, make_app, run

STEAM = "progress: *([0-9]+(?:[.][0-9]+)?)"
DOWNLOAD = "Update state (0x61) downloading, progress: {p} ({a} / 4892200000)"
BASE = {"id": "g", "name": "G", "image": "img:1"}


# ------------------------------------------------------------------ pure helpers
def test_strip_ansi_removes_colours_and_cursor_codes():
    raw = "\x1b[31mred\x1b[0m \x1b[1;32mgreen\x1b[0m\x1b[2K\x1b]0;title\x07done"
    assert rd.strip_ansi(raw) == "red greendone"


def test_clean_log_text_keeps_the_last_version_of_a_rewritten_line():
    raw = "start\nprogress 10%\rprogress 50%\rprogress 90%\nend\r\n"
    assert rd.clean_log_text(raw) == "start\nprogress 90%\nend\n"
    assert "progress 10%" in rd.clean_log_text(raw, collapse_cr=False)


def test_clean_log_text_drops_control_characters():
    assert rd.clean_log_text("a\x00b\x07c\td") == "abc\td"


def test_last_progress_finds_the_newest_percentage():
    text = "\n".join([DOWNLOAD.format(p="10.5", a=1), "noise", DOWNLOAD.format(p="51.68", a=2)])
    assert rd.last_progress(text, STEAM) == 51.68


def test_last_progress_understands_carriage_return_updates_in_one_line():
    text = DOWNLOAD.format(p="1.00", a=1) + "\r" + DOWNLOAD.format(p="2.50", a=2) + "\r" + DOWNLOAD.format(p="3.75", a=3)
    assert rd.last_progress(text, STEAM) == 3.75


def test_last_progress_ignores_old_progress_once_other_lines_followed():
    later = "\n".join(f"server line {i}" for i in range(rd.PROGRESS_WINDOW_LINES + 5))
    assert rd.last_progress(DOWNLOAD.format(p="99.9", a=1) + "\n" + later, STEAM) is None


@pytest.mark.parametrize("pattern", ["", "(", "x"])
def test_last_progress_without_a_usable_pattern(pattern):
    assert rd.last_progress("progress: 12", pattern) is None


def test_last_progress_rejects_values_out_of_range():
    assert rd.last_progress("progress: 250", STEAM) is None


def test_pattern_seen():
    assert rd.pattern_seen("x\n\x1b[32mRunning Palworld dedicated server on :8211\x1b[0m\n", "Running Palworld dedicated server on")
    assert not rd.pattern_seen("nothing", "Running")
    assert not rd.pattern_seen("anything", "")
    assert not rd.pattern_seen("anything", "(")  # an invalid expression never raises


@pytest.mark.parametrize("health,has_pat,seen,prog,phase", [
    ("healthy", False, False, None, "ready"),
    ("healthy", True, False, 40.0, "ready"),            # healthy wins
    ("", False, False, None, "ready"),                  # no signal at all: running means ready
    ("", False, False, 30.0, "installing"),             # ...unless the log shows a download in progress
    ("", False, False, 100.0, "ready"),
    ("", True, False, None, "starting"),
    ("", True, True, None, "ready"),
    ("starting", False, False, None, "starting"),
    ("starting", False, False, 51.68, "installing"),
    ("starting", True, True, None, "ready"),            # the ready line beats a health check that is still starting
    ("unhealthy", False, False, None, "unhealthy"),
    ("unhealthy", False, False, 12.0, "installing"),    # failing checks during a long download are not a fault
    ("unhealthy", True, True, None, "ready"),
])
def test_decide(health, has_pat, seen, prog, phase):
    r = rd.decide(health, has_pat, seen, prog)
    assert r.phase == phase
    assert r.ready == (phase == "ready")
    assert (r.progress == prog) if phase == "installing" else r.progress is None
    if phase != "ready":
        assert r.detail


# ------------------------------------------------------------------ catalog fields
def test_catalog_accepts_patterns_and_round_trips_them():
    from gamedock.catalog import entry_to_dict
    e = parse_definition({**BASE, "ready_log_pattern": "Server ready", "progress_log_pattern": STEAM})
    assert e.ready_log_pattern == "Server ready" and e.progress_log_pattern == STEAM
    assert parse_definition(entry_to_dict(e)) == e


def test_catalog_patterns_are_optional():
    e = parse_definition(BASE)
    assert e.ready_log_pattern == "" and e.progress_log_pattern == ""


@pytest.mark.parametrize("extra,msg", [
    ({"ready_log_pattern": "("}, "regular expression"),
    ({"progress_log_pattern": "progress: ([0-9]+"}, "regular expression"),
    ({"progress_log_pattern": "progress: [0-9]+"}, "capture group"),
    ({"progress_log_pattern": "(a)(b)"}, "capture group"),
    ({"ready_log_pattern": "a\nb"}, "one line"),
    ({"ready_log_pattern": "x" * 400}, "one line"),
    ({"ready_log_pattern": 5}, "text"),
])
def test_catalog_rejects_bad_patterns(extra, msg):
    with pytest.raises(CatalogError, match=msg):
        parse_definition({**BASE, **extra})


def test_shipped_catalog_patterns_are_valid_and_match_real_lines():
    from pathlib import Path
    from gamedock.catalog import load_catalog
    cat = load_catalog(Path(__file__).resolve().parent.parent / "catalog")
    pal, ens = cat["palworld"], cat["enshrouded"]
    assert rd.pattern_seen("Running Palworld dedicated server on :8211", pal.ready_log_pattern)
    assert rd.pattern_seen("[Session] 'HostOnline' (up)!", ens.ready_log_pattern)
    for e in (pal, ens):
        assert rd.last_progress(DOWNLOAD.format(p="51.68", a=2528448160), e.progress_log_pattern) == 51.68


# ------------------------------------------------------------------ service
GAME = textwrap.dedent("""\
    id: slow
    name: Slow Game
    image: img:1
    ready_log_pattern: "Running slow server on"
    progress_log_pattern: "progress: *([0-9]+(?:[.][0-9]+)?)"
    ports:
      - { protocol: udp, host: 7000, container: 7000 }
    fields:
      - { name: ADMIN_PASSWORD, label: Admin password, secret: true }
    """)
PLAIN = textwrap.dedent("""\
    id: plain
    name: Plain Game
    image: img:2
    ports:
      - { protocol: udp, host: 7001, container: 7001 }
    fields:
      - { name: ADMIN_PASSWORD, label: Admin password, secret: true }
    """)


@pytest.fixture
def svc(backend, tmp_path, clock, monkeypatch):
    monkeypatch.setattr(service_module, "READINESS_TTL", 0.0)
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "slow.yaml").write_text(GAME)
    (cat / "plain.yaml").write_text(PLAIN)
    return GameDockService(backend, cat, tmp_path / "data", clock=clock)


def logs_calls(backend):
    return sum(1 for a, _ in backend.calls if a == "logs")


def test_a_game_with_no_signals_is_ready_as_soon_as_it_runs(svc):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    s = svc.get_status("p")
    assert (s.status, s.phase, s.ready) == (Status.STOPPED, "", False)
    s = svc.start("p")
    assert (s.status, s.phase, s.ready, s.progress) == (Status.RUNNING, "ready", True, None)


def test_ready_pattern_decides_when_there_is_no_health_check(svc, backend):
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    s = svc.start("s")
    assert (s.status, s.phase, s.ready) == (Status.RUNNING, "starting", False)
    backend.append_log("gamedock-s", "Loading world...", "Running slow server on :7000")
    s = svc.get_status("s")
    assert (s.phase, s.ready) == ("ready", True)


def test_ready_line_is_remembered_after_it_scrolls_out_of_the_log(svc, backend):
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("s")
    backend.append_log("gamedock-s", "Running slow server on :7000")
    assert svc.get_status("s").ready
    backend.set_logs("gamedock-s", "\n".join(f"noise {i}" for i in range(500)))
    before = logs_calls(backend)
    assert svc.get_status("s").ready
    assert logs_calls(backend) == before  # no need to read the log again


def test_a_restart_forgets_the_ready_line(svc, backend):
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("s")
    backend.append_log("gamedock-s", "Running slow server on :7000")
    assert svc.get_status("s").ready
    svc.stop("s")
    backend.set_logs("gamedock-s", "")
    time.sleep(0.002)  # the fake stamps each start with the wall clock
    s = svc.start("s")
    assert s.phase == "starting" and not s.ready


def test_download_progress_is_reported_while_installing(svc, backend):
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("s")
    backend.append_log("gamedock-s", "Steam Console Client", DOWNLOAD.format(p="51.68", a=2528448160))
    s = svc.get_status("s")
    assert (s.phase, s.progress, s.ready) == ("installing", 51.68, False)
    assert "51.68" in s.phase_detail
    backend.append_log("gamedock-s", DOWNLOAD.format(p="100.00", a=4892200000))
    assert svc.get_status("s").phase == "starting"


def test_docker_health_check_is_authoritative(svc, backend):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("p")
    backend.health["gamedock-p"] = "starting"
    s = svc.get_status("p")
    assert (s.phase, s.ready) == ("starting", False)
    backend.health["gamedock-p"] = "unhealthy"
    assert svc.get_status("p").phase == "unhealthy"
    backend.health["gamedock-p"] = "healthy"
    s = svc.get_status("p")
    assert (s.phase, s.ready) == ("ready", True)


def test_failing_health_check_during_a_download_is_shown_as_installing(svc, backend):
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("s")
    backend.health["gamedock-s"] = "unhealthy"
    backend.append_log("gamedock-s", DOWNLOAD.format(p="20.00", a=1))
    assert svc.get_status("s").phase == "installing"


def test_healthy_containers_cost_no_log_reads(svc, backend):
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("s")
    backend.health["gamedock-s"] = "healthy"
    before = logs_calls(backend)
    for _ in range(5):
        assert svc.list_servers()[0].ready
    assert logs_calls(backend) == before


def test_log_reads_are_throttled(backend, tmp_path, clock, monkeypatch):
    monkeypatch.setattr(service_module, "READINESS_TTL", 60.0)
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "slow.yaml").write_text(GAME)
    svc = GameDockService(backend, cat, tmp_path / "data", clock=clock)
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("s")
    for _ in range(10):
        svc.list_servers()
    assert logs_calls(backend) == 1


def test_a_broken_log_read_never_breaks_the_server_list(svc, backend):
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("s")
    backend.fail_next = "logs"
    s = svc.list_servers()[0]
    assert s.status == Status.RUNNING and s.phase == "starting"


def test_stopped_servers_have_no_phase(svc, backend):
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("s")
    s = svc.stop("s")
    assert (s.phase, s.ready, s.progress) == ("", False, None)


def test_phase_is_in_the_api_and_the_mcp_status(svc, backend):
    svc.deploy("slow", "S", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("s")
    backend.append_log("gamedock-s", DOWNLOAD.format(p="51.68", a=1))
    web = TestClient(create_app(svc)).get("/api/servers/s").json()
    assert web["status"] == "running" and web["phase"] == "installing" and web["progress"] == 51.68
    assert web["ready"] is False and "51.68" in web["phase_detail"]

    async def go(c):
        return (await call(c, "get_server_status", server_id="s"))[1], (await call(c, "list_servers"))[1]

    one, many = run(make_app(svc), go)
    assert one["phase"] == "installing" and one["progress"] == 51.68 and one["ready"] is False
    assert many["servers"][0]["phase"] == "installing"


# ------------------------------------------------------------------ what GameDock is doing right now
def test_activity_is_visible_during_a_slow_stop(svc, backend):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("p")
    backend.delays["stop"] = 0.6
    t = threading.Thread(target=svc.stop, args=("p",))
    t.start()
    time.sleep(0.2)
    assert svc.get_status("p").activity == "stopping"
    assert svc.list_servers()[0].activity == "stopping"
    t.join()
    assert svc.get_status("p").activity == ""


def test_the_answer_to_an_action_does_not_claim_the_action_is_still_running(svc):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    assert svc.start("p").activity == ""


def test_a_deploy_in_progress_is_listed_as_deploying(svc, backend):
    backend.delays["create"] = 0.6
    t = threading.Thread(target=svc.deploy, args=("plain", "P", None, {"ADMIN_PASSWORD": "secret1"}))
    t.start()
    time.sleep(0.2)
    try:
        [s] = svc.list_servers()
        assert (s.id, s.name, s.activity, s.status) == ("p", "P", "deploying", Status.NOT_CREATED)
        assert svc.get_status("p").activity == "deploying"
        assert "secret1" not in repr(s)
    finally:
        t.join()
    [s] = svc.list_servers()
    assert s.activity == "" and s.status == Status.STOPPED


def test_a_failed_deploy_leaves_no_ghost_entry(svc, backend):
    backend.fail_next = "create"
    with pytest.raises(Exception):
        svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    assert svc.list_servers() == []


# ------------------------------------------------------------------ logs
def test_logs_strip_colours_and_return_the_newest_lines(svc, backend):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("p")
    backend.set_logs("gamedock-p", "\n".join(["\x1b[32mline %d\x1b[0m" % i for i in range(1, 11)]))
    r = svc.get_logs("p", 3)
    assert r.text == "line 8\nline 9\nline 10"
    assert (r.lines, r.truncated, r.state) == (3, True, "running")
    r = svc.get_logs("p", 50)
    assert r.lines == 10 and not r.truncated and "\x1b" not in r.text


def test_logs_hide_secret_setting_values(svc, backend):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "hunter2"})
    svc.start("p")
    backend.set_logs("gamedock-p", "starting\nadmin password is hunter2 ok")
    r = svc.get_logs("p")
    assert "hunter2" not in r.text and "admin password is ******** ok" in r.text


def test_logs_work_for_a_stopped_server_and_say_so_when_there_is_no_container(svc, backend):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    backend.remove("gamedock-p")
    r = svc.get_logs("p")
    assert r.state == "not_created" and r.text == "" and "no container" in r.note
    svc.start("p")  # re-creates it
    svc.stop("p")
    r = svc.get_logs("p")
    assert r.state == "stopped" and "container started" in r.text


def test_logs_empty_output_is_explained(svc, backend):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("p")
    backend.set_logs("gamedock-p", "")
    r = svc.get_logs("p")
    assert r.text == "" and r.lines == 0 and "not written anything" in r.note


def test_logs_size_cap_keeps_the_newest_whole_lines(svc, backend):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("p")
    backend.set_logs("gamedock-p", "\n".join(f"{i:04d} " + "x" * 90 for i in range(500)))
    r = svc.get_logs("p", 500, max_chars=2000)
    assert len(r.text) <= 2000 and r.truncated
    assert r.text.splitlines()[-1].startswith("0499") and r.text.splitlines()[0].startswith("04")
    assert all(len(l) == 95 for l in r.text.splitlines())  # no half line at the start


@pytest.mark.parametrize("n", [0, -1, 1001])
def test_logs_reject_silly_line_counts(svc, n):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    with pytest.raises(ValidationError):
        svc.get_logs("p", n)


def test_logs_endpoint(svc, backend):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "hunter2"})
    svc.start("p")
    backend.set_logs("gamedock-p", "a\n\x1b[31mb\x1b[0m hunter2")
    c = TestClient(create_app(svc))
    r = c.get("/api/servers/p/logs?lines=5").json()
    assert r["text"] == "a\nb ********" and r["lines"] == 2 and r["state"] == "running"
    assert c.get("/api/servers/p/logs").status_code == 200
    assert c.get("/api/servers/p/logs?lines=0").status_code == 400
    assert c.get("/api/servers/nope/logs").status_code == 404


def test_logs_mcp_tool_is_read_only_and_capped(svc, backend):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "hunter2"})
    svc.start("p")
    backend.set_logs("gamedock-p", "\n".join(f"row {i} hunter2" for i in range(300)))

    async def go(c):
        tools = {t.name: t for t in (await c.list_tools()).tools}
        err, r = await call(c, "get_server_logs", server_id="p", lines=5)
        err2, big = await call(c, "get_server_logs", server_id="p", lines=5000)
        err3, dflt = await call(c, "get_server_logs", server_id="p")
        return tools, (err, r), (err2, big), (err3, dflt)

    for mode in ("read_only", "standard", "full"):
        tools, (err, r), (err2, big), (err3, dflt) = run(make_app(svc, mode=mode), go)
        assert tools["get_server_logs"].annotations.read_only_hint is True
        assert not err and r["lines"] == 5 and "hunter2" not in r["text"] and "row 299" in r["text"]
        assert err2  # more than 500 lines is refused by the tool's schema
        assert not err3 and dflt["lines"] == 100


def test_a_server_deployed_before_the_patterns_existed_uses_the_current_catalog_patterns(svc, backend, tmp_path):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})  # its template snapshot has no patterns
    svc.start("p")
    assert svc.get_status("p").phase == "ready"
    (tmp_path / "cat" / "plain.yaml").write_text(PLAIN + 'ready_log_pattern: "Now joinable"\n'
                                                 'progress_log_pattern: "dl ([0-9.]+)"\n')
    svc._entries_cache = None  # the catalog is re-read at most every 30 s
    backend.health["gamedock-p"] = "unhealthy"  # as Palworld's check does during a long download
    backend.append_log("gamedock-p", "dl 40.0")
    s = svc.get_status("p")
    assert (s.phase, s.progress) == ("installing", 40.0)
    backend.append_log("gamedock-p", "Now joinable")
    assert svc.get_status("p").ready


def test_the_catalog_fallback_ignores_a_game_that_left_the_catalog(svc, backend, tmp_path):
    svc.deploy("plain", "P", env={"ADMIN_PASSWORD": "secret1"})
    svc.start("p")
    (tmp_path / "cat" / "plain.yaml").unlink()
    svc._entries_cache = None
    assert svc.get_status("p").phase == "ready"


def test_version_is_consistent(svc):
    import re
    from pathlib import Path
    import gamedock
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert re.search(r'^version = "%s"$' % re.escape(gamedock.__version__), pyproject, re.M)
    assert gamedock.__version__ == "0.4.0"
