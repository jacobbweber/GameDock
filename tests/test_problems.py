"""0.6.1: known problems explained on the card, in Check setup, diagnostics and MCP (fail_log_patterns, log_files, built-in
patterns), field format rules (pattern), and the bundled ASKA example entry."""
import json
import os
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamedock import authoring as au
from gamedock import problems as pb
from gamedock.catalog import entry_to_dict, load_catalog, parse_definition
from gamedock.errors import CatalogError, ValidationError
from gamedock.lint import lint_catalog
from gamedock.models import FailPattern
from gamedock.problem_service import read_tail
from gamedock.service import GameDockService
from gamedock.web.app import create_app
from tests.mcp_helpers import call, make_app, run

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "0123456789ABCDEF0123456789ABCDEF"
SECRET_PW = "hunter2-very-secret"

GAME = textwrap.dedent("""\
    id: tokgame
    name: Token Game
    image: example/tokgame:1
    ports:
      - { protocol: udp, host: 29015, container: 29015 }
    volumes:
      - { host: ./server, container: /srv/server }
    fields:
      - { name: SERVER_NAME, label: Server name, default: "Tok", required: true }
      - name: AUTH_TOKEN
        label: Login token
        secret: true
        required: true
        pattern: '[0-9A-Fa-f]{32}'
        pattern_hint: It is exactly 32 letters and digits (0-9, A-F).
      - { name: ADMIN_PASSWORD, label: Admin password, secret: true }
    ready_log_pattern: "session is open"
    fail_log_patterns:
      - pattern: 'CustomAuthenticationFailed|k_EResultAccountNotFound'
        title: Steam rejected the login token (CustomAuthenticationFailed).
        advice: Create a new token for App ID 1898300 (not a Web API key), then Edit the server and paste it.
    log_files: [server/logs/game.log]
    """)
ENV = {"SERVER_NAME": "Mine", "AUTH_TOKEN": TOKEN, "ADMIN_PASSWORD": SECRET_PW}
FILE_LOG = "[10:00:01] Failed to connect to Steam reason: k_EResultAccountNotFound\n[10:00:02] Disconnected with reason: CustomAuthenticationFailed\n"


@pytest.fixture
def svc(backend, catalog_dir, tmp_path, clock):
    (catalog_dir / "tokgame.yaml").write_text(GAME)
    return GameDockService(backend, catalog_dir, tmp_path / "data", clock=clock)


def game_log(svc, text=FILE_LOG, fresh=True):
    p = svc.data_dir / "tok" / "server" / "logs" / "game.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if not fresh:
        os.utime(p, (1, 1))
    return p


def start(svc, backend, health="unhealthy", logs="booting\n"):
    svc.deploy("tokgame", "Tok", env=ENV, start=True)
    backend.health["gamedock-tok"] = health
    backend.set_logs("gamedock-tok", logs)


# ---------------------------------------------------------------------------------------------- pure patterns
def test_catalog_pattern_beats_builtin_and_shows_the_newest_line():
    fp = FailPattern("CustomAuthenticationFailed", "Steam rejected the token.", "Make a new one.")
    p = pb.scan([("the container log", "x\nCustomAuthenticationFailed once\nok\nCustomAuthenticationFailed twice\n")], [fp])
    assert p.source == "catalog" and p.title == "Steam rejected the token." and p.excerpt.endswith("twice")
    assert p.where == "the container log" and p.advice == "Make a new one."
    # without the catalog pattern the built-in Steam token pattern still explains it
    q = pb.scan([("the container log", "CustomAuthenticationFailed\n")], [])
    assert q.source == "builtin" and q.id == "steam-token"


@pytest.mark.parametrize("line,pid", [
    ("Error! App '3246670' state is 0x202 after update job.", "steam-state"),
    ("ERROR! Failed to install app '1234' (Disk write failure)", "steam-install-failed"),
    ("ERROR! Failed to install app '1234' (No subscription)", "steam-no-subscription"),
    ("Steam: No subscription", "steam-no-subscription"),
    ("Error: Missing configuration", "steam-missing-config"),
    ("FAILED (Invalid Password)", "steam-login"),
    ("Login Failure: Rate Limit Exceeded", "steam-login"),
    ("Failed to connect to Steam reason: k_EResultAccountNotFound", "steam-token"),
    ("System.OutOfMemoryException: out of memory", "out-of-memory"),
    ("bind: Address already in use", "port-in-use"),
    ("Could not listen: port 27015 is already in use", "port-in-use"),
    ("mkdir: cannot create directory '/home/steam/server/logs': Permission denied", "permission-denied"),
    ("Error opening file: permission denied /saves/world.db", "permission-denied"),
    ("write failed: No space left on device", "disk-full"),
])
def test_builtin_patterns_name_the_failure(line, pid):
    p = pb.scan([("the container log", f"starting\n{line}\nmore\n")])
    assert p is not None and p.id == pid, (line, p)
    assert p.source == "builtin" and p.title and p.advice


@pytest.mark.parametrize("line", [
    "Server started on port 27015", "Loading world... 45%", "player Alice joined (permission level: admin)",
    "Permission denied hints are disabled", "Memory: 1024 MB used", "Update state (0x61) downloading, progress: 51.68",
    "Success! App '3246670' fully installed.", "Login Success", "Connected to Steam", "disk usage: 40% full",
    "listening on address 0.0.0.0:27015",
])
def test_builtin_patterns_stay_quiet_on_normal_lines(line):
    assert pb.scan([("the container log", line + "\n")]) is None


def test_builtin_patterns_can_be_switched_off_and_never_leak_secrets():
    assert pb.scan([("log", "No space left on device\n")], use_builtin=False) is None
    p = pb.scan([("log", f"CustomAuthenticationFailed token={TOKEN} pw {SECRET_PW}\n")], secrets=[SECRET_PW])
    assert TOKEN not in p.excerpt and SECRET_PW not in p.excerpt
    long = pb.scan([("log", "No space left on device " + "x" * 5000 + "\n")])
    assert len(long.excerpt) <= pb.MAX_EXCERPT


def test_docker_level_problems():
    assert pb.docker_problem(137, True).id == "out-of-memory"
    assert pb.docker_problem(137, False).id == "killed"
    assert pb.docker_problem(1, False) is None and pb.docker_problem(None, False) is None


def test_ansi_and_carriage_returns_do_not_hide_a_match():
    p = pb.scan([("log", "\x1b[31mNo space left\x1b[0m on device\r\n")])
    assert p is not None and p.id == "disk-full" and "\x1b" not in p.excerpt


# ---------------------------------------------------------------------------------------------- catalog keys
def entry(extra: str):
    return parse_definition(__import__("yaml").safe_load(GAME + extra), "t")


def test_new_keys_round_trip_and_defaults():
    e = entry("")
    assert e.fail_log_patterns[0].title.startswith("Steam rejected") and e.log_files == ("server/logs/game.log",)
    assert e.builtin_fail_patterns is True
    assert parse_definition(entry_to_dict(e), "again") == e
    f = {x.name: x for x in e.fields}["AUTH_TOKEN"]
    assert f.pattern == "[0-9A-Fa-f]{32}" and "32 letters" in f.pattern_hint


@pytest.mark.parametrize("snippet,msg", [
    ("fail_log_patterns: nope\n", "list"),
    ("fail_log_patterns: [{pattern: 'x(', title: t, advice: a}]\n", "regular expression"),
    ("fail_log_patterns: [{pattern: x, title: '', advice: a}]\n", "needs a title"),
    ("fail_log_patterns: [{pattern: x, title: t}]\n", "needs a title"),
    ("fail_log_patterns: [{pattern: '', title: t, advice: a}]\n", "needs a pattern"),
    ("fail_log_patterns: [{pattern: x, title: t, advice: a, extra: 1}]\n", "does not know"),
    ("fail_log_patterns: [{pattern: '(a+)+$', title: t, advice: a}]\n", "repeats a group"),
    ("fail_log_patterns: [{pattern: '(.*)*x', title: t, advice: a}]\n", "repeats a group"),
    ("fail_log_patterns: [{pattern: x, title: t, advice: 'a'}, 5]\n", "must have pattern"),
    ("log_files: [/etc/passwd]\n", "relative"),
    ("log_files: ['../x.log']\n", "relative"),
    ("log_files: 'a.log'\n", "must be a list"),
    ("builtin_fail_patterns: maybe\n", "true or false"),
])
def test_bad_new_keys_are_refused_with_a_plain_message(snippet, msg):
    base = GAME.replace(GAME[GAME.index("fail_log_patterns:"):], "")
    with pytest.raises(CatalogError, match=msg):
        parse_definition(__import__("yaml").safe_load(base + snippet), "t")


def test_too_many_patterns_and_files_are_refused():
    base = GAME.replace(GAME[GAME.index("fail_log_patterns:"):], "")
    many = "fail_log_patterns:\n" + "".join(f"  - {{pattern: 'e{i}', title: t, advice: a}}\n" for i in range(13))
    with pytest.raises(CatalogError, match="at most 12"):
        parse_definition(__import__("yaml").safe_load(base + many), "t")
    with pytest.raises(CatalogError, match="at most 6"):
        parse_definition(__import__("yaml").safe_load(base + "log_files: [" + ",".join(f"a{i}.log" for i in range(7)) + "]\n"), "t")


def test_field_pattern_rules_are_validated_when_loaded():
    bad = GAME.replace("'[0-9A-Fa-f]{32}'", "'[0-9A-F'")
    with pytest.raises(CatalogError, match="regular expression"):
        parse_definition(__import__("yaml").safe_load(bad), "t")
    nohint = GAME.replace("    pattern_hint: It is exactly 32 letters and digits (0-9, A-F).\n", "    pattern_hint: ''\n")
    assert parse_definition(__import__("yaml").safe_load(nohint), "t")          # allowed, but linted
    orphan = GAME.replace("    pattern: '[0-9A-Fa-f]{32}'\n", "")
    with pytest.raises(CatalogError, match="no pattern"):
        parse_definition(__import__("yaml").safe_load(orphan), "t")
    wrong_default = GAME.replace("secret: true\n    required: true", "secret: false\n    default: nope\n    required: true")
    with pytest.raises(CatalogError, match="does not match its own pattern"):
        parse_definition(__import__("yaml").safe_load(wrong_default), "t")


def test_strict_mode_accepts_the_new_keys_and_knows_their_sub_keys():
    assert au.check_text(GAME).ok
    bad = au.check_text(GAME.replace("    title: Steam rejected", "    colour: red\n    title: Steam rejected"))
    assert not bad.ok and any("fail_log_patterns" in e for e in bad.errors)


def test_validate_flags_vague_or_too_broad_rules():
    broad = GAME.replace("'CustomAuthenticationFailed|k_EResultAccountNotFound'", "'.*'")
    flags = au.analyze_entry(au.check_text(broad).entry)
    assert any("matches ordinary log lines" in f.text for f in flags)
    nohint = GAME.replace("    pattern_hint: It is exactly 32 letters and digits (0-9, A-F).\n", "")
    assert any("no pattern_hint" in f.text for f in au.analyze_entry(au.check_text(nohint).entry))
    # a log file that is not inside a mounted folder can never be read
    outside = GAME.replace("server/logs/game.log", "elsewhere/game.log")
    assert any("not inside any mounted folder" in f.text for f in au.analyze_entry(au.check_text(outside).entry))
    # a required secret without a rule is only a note
    plain = GAME.replace("    pattern: '[0-9A-Fa-f]{32}'\n    pattern_hint: It is exactly 32 letters and digits (0-9, A-F).\n", "")
    assert any("format check" in f.text and f.level == "info" for f in au.analyze_entry(au.check_text(plain).entry))


def test_review_summary_names_log_files_and_patterns():
    s = au.summarize_entry(au.check_text(GAME).entry)
    text = " ".join(s["lines"])
    assert "server/logs/game.log" in text and "Steam rejected the login token" in text and "AUTH_TOKEN" in text
    assert s["log_files"] == ["server/logs/game.log"]


# ---------------------------------------------------------------------------------------------- format rule (pattern)
def test_deploy_refuses_a_wrong_token_without_echoing_it(svc):
    bad = "NOT-A-TOKEN-" + "z" * 20
    with pytest.raises(ValidationError) as ei:
        svc.deploy("tokgame", "Tok", env={**ENV, "AUTH_TOKEN": bad})
    msg = str(ei.value)
    assert "Login token" in msg and "exactly 32 letters and digits" in msg and bad not in msg and "z" * 6 not in msg
    assert svc.list_servers() == []
    for wrong in ("0123", TOKEN + "0", "G" * 32, TOKEN[:31] + " "):
        with pytest.raises(ValidationError):
            svc.deploy("tokgame", "Tok", env={**ENV, "AUTH_TOKEN": wrong + "q" if wrong == TOKEN[:31] + " " else wrong})


def test_stray_spaces_around_a_good_token_are_trimmed(svc):
    svc.deploy("tokgame", "Tok", env={**ENV, "AUTH_TOKEN": f"  {TOKEN}  "})
    assert svc.store.get("tok").env["AUTH_TOKEN"] == TOKEN


def test_edit_checks_a_new_token_and_a_blank_keeps_the_old_one(svc, backend):
    svc.deploy("tokgame", "Tok", env=ENV)
    with pytest.raises(ValidationError, match="exactly 32"):
        svc.update("tok", env={"AUTH_TOKEN": "short"})
    svc.update("tok", env={"AUTH_TOKEN": ""})                       # blank = keep (never checked)
    assert svc.store.get("tok").env["AUTH_TOKEN"] == TOKEN
    new = "FEDCBA9876543210FEDCBA9876543210"
    svc.update("tok", env={"AUTH_TOKEN": new})
    assert svc.store.get("tok").env["AUTH_TOKEN"] == new


def test_server_deployed_before_the_rule_existed_is_checked_with_the_current_rule(svc, backend, catalog_dir):
    old = GAME.replace("    pattern: '[0-9A-Fa-f]{32}'\n    pattern_hint: It is exactly 32 letters and digits (0-9, A-F).\n", "")
    old = old.replace(old[old.index("fail_log_patterns:"):], "")
    (catalog_dir / "tokgame.yaml").write_text(old)
    svc.deploy("tokgame", "Tok", env={**ENV, "AUTH_TOKEN": "anything goes"})
    assert "pattern" in json.dumps(svc.store.get("tok").template) and svc.store.get("tok").template["fields"][1]["pattern"] == ""
    (catalog_dir / "tokgame.yaml").write_text(GAME)                  # the catalog entry gains the rule
    with pytest.raises(ValidationError, match="exactly 32"):
        svc.update("tok", env={"AUTH_TOKEN": "still wrong"})
    svc.update("tok", env={"AUTH_TOKEN": TOKEN})
    drift = svc.template_drift(svc.store.get("tok"), svc.get_catalog_entry("tokgame"))
    assert drift == []     # the rule and the problem hints are not "the entry changed": they apply to old servers anyway


def test_preview_deploy_reports_the_wrong_token(svc):
    r = svc.preview_deploy("tokgame", "Tok", None, {**ENV, "AUTH_TOKEN": "nope"})
    assert not r["ok"] and any("exactly 32" in str(p) for p in r["problems"]) and "nope" not in json.dumps(r)


# ---------------------------------------------------------------------------------------------- the card
def test_unhealthy_server_shows_the_catalog_problem_from_the_game_log_file(svc, backend):
    start(svc, backend)
    game_log(svc)
    s = svc.get_status("tok")
    assert s.phase == "unhealthy" and s.problem["id"] == "catalog-1" and s.problem["source"] == "catalog"
    assert s.problem["title"].startswith("Steam rejected the login token")
    assert "CustomAuthenticationFailed" in s.problem["excerpt"] and "server/logs/game.log" in s.problem["where"]
    assert "Steam rejected the login token" in s.phase_detail and "health check" not in s.phase_detail
    assert TOKEN not in json.dumps(s.problem)


def test_problem_from_the_container_log_and_secret_redaction(svc, backend):
    start(svc, backend, logs=f"auth with {TOKEN} and pw {SECRET_PW}\nDisconnected: CustomAuthenticationFailed ({TOKEN})\n")
    s = svc.get_status("tok")
    assert s.problem["where"] == "the container log"
    assert TOKEN not in json.dumps(s.problem) and SECRET_PW not in json.dumps(s.problem)


def test_unhealthy_without_a_known_problem_gets_the_clearer_generic_message(svc, backend):
    start(svc, backend, logs="booting\nloading\n")
    s = svc.get_status("tok")
    assert s.phase == "unhealthy" and s.problem == {}
    assert "Open Logs" in s.phase_detail and "Check setup" in s.phase_detail and "may still be starting" not in s.phase_detail


def test_starting_and_installing_servers_show_a_problem_too_but_keep_their_phase(svc, backend):
    start(svc, backend, health="starting", logs="Error! App '1' state is 0x202 after update job.\n")
    s = svc.get_status("tok")
    assert s.phase == "starting" and s.problem["id"] == "steam-state"
    assert "Downloading" not in s.phase_detail and "not ready" in s.phase_detail


def test_ready_servers_never_show_a_problem_even_with_old_error_lines(svc, backend):
    start(svc, backend, health="healthy", logs="No space left on device\nCustomAuthenticationFailed\n")
    game_log(svc)
    assert svc.get_status("tok").problem == {}
    backend.health["gamedock-tok"] = "unhealthy"
    backend.set_logs("gamedock-tok", "session is open\nNo space left on device\n")
    assert svc.get_status("tok").problem == {}                       # the ready line was seen


def test_a_log_file_from_an_older_run_is_ignored(svc, backend, clock):
    start(svc, backend)
    game_log(svc, fresh=False)                                       # last written long before this start
    assert svc.get_status("tok").problem == {}


def test_log_file_reading_is_confined_to_the_data_folder(svc, backend, tmp_path):
    start(svc, backend)
    outside = tmp_path / "outside.log"
    outside.write_text(FILE_LOG)
    base = svc.data_dir / "tok"
    link = base / "server" / "logs"
    link.mkdir(parents=True, exist_ok=True)
    (link / "game.log").symlink_to(outside)                          # a symlink out of the data folder
    assert svc.get_status("tok").problem == {}
    assert read_tail(base / "server" / "logs" / "game.log", base) is None
    assert read_tail(base / ".." / "outside.log", base) is None
    assert read_tail(base / "nothing.log", base) is None
    d = base / "server" / "dir.log"
    d.mkdir()
    assert read_tail(d, base) is None


def test_log_file_tail_only_and_no_partial_first_line(tmp_path):
    base = tmp_path
    p = base / "big.log"
    p.write_text("OLD LINE no space left on device\n" + ("filler line\n" * 20000) + "last line\n")
    text, _ = read_tail(p, base, 4096)
    assert len(text) <= 4096 and "OLD LINE" not in text and text.rstrip().endswith("last line")
    assert text.startswith("filler line")


def test_patterns_come_from_the_current_catalog_for_servers_with_an_old_snapshot(svc, backend, catalog_dir):
    old = GAME.replace(GAME[GAME.index("fail_log_patterns:"):GAME.index("log_files:")], "")
    (catalog_dir / "tokgame.yaml").write_text(old)
    start(svc, backend)
    assert "fail_log_patterns" not in svc.store.get("tok").template or svc.store.get("tok").template["fail_log_patterns"] == []
    game_log(svc)
    assert svc.get_status("tok").problem.get("source") == "builtin"   # only the built-in Steam pattern, no catalog entry yet
    (catalog_dir / "tokgame.yaml").write_text(GAME)
    svc._entries_cache = None
    svc._problem_cache.clear()
    s = svc.get_status("tok")
    assert s.problem["source"] == "catalog" and s.problem["title"].startswith("Steam rejected")


def test_problem_hints_refresh_even_when_the_snapshot_has_them(svc, backend, catalog_dir):
    start(svc, backend)
    game_log(svc)
    assert svc.store.get("tok").template["fail_log_patterns"][0]["title"].startswith("Steam rejected")
    (catalog_dir / "tokgame.yaml").write_text(GAME.replace("Steam rejected the login token (CustomAuthenticationFailed).", "NEW WORDING."))
    svc._entries_cache = None
    svc._problem_cache.clear()
    assert svc.get_status("tok").problem["title"] == "NEW WORDING."
    # the entry is switched off for the built-ins
    (catalog_dir / "tokgame.yaml").write_text(GAME.replace("log_files: [server/logs/game.log]", "log_files: [server/logs/game.log]\nbuiltin_fail_patterns: false")
                                              .replace("'CustomAuthenticationFailed|k_EResultAccountNotFound'", "'never-matches-xyz'"))
    svc._entries_cache = None
    svc._problem_cache.clear()
    assert svc.get_status("tok").problem == {}


def test_answers_are_cached_so_polling_does_not_hammer_docker(svc, backend):
    start(svc, backend)
    game_log(svc)
    svc.get_status("tok")
    n = sum(1 for c in backend.calls if c[0] == "logs")
    for _ in range(10):
        svc.list_servers()
    assert sum(1 for c in backend.calls if c[0] == "logs") - n <= 1


def test_killed_for_memory_shows_on_a_stopped_card(svc, backend):
    svc.deploy("tokgame", "Tok", env=ENV, start=True)
    svc.stop("tok")
    backend.exit_info["gamedock-tok"] = (137, True)
    s = svc.get_status("tok")
    assert s.status.value == "stopped" and s.problem["id"] == "out-of-memory" and s.problem["source"] == "docker"
    backend.exit_info["gamedock-tok"] = (137, False)                 # exit 137 alone is not shown on the card (a slow stop looks the same)
    assert svc.get_status("tok").problem == {}
    backend.exit_info["gamedock-tok"] = (0, False)
    assert svc.get_status("tok").problem == {}


# ---------------------------------------------------------------------------------------------- Check setup, diagnostics
def by_id(r):
    return {c["id"]: c for c in r["checks"]}


def test_check_setup_names_the_problem_with_the_next_step(svc, backend):
    start(svc, backend)
    game_log(svc)
    r = svc.verify_game_setup("tok")
    c = by_id(r)["problem"]
    assert c["status"] == "fail" and c["title"] == "Known problem" and "Steam rejected the login token" in c["detail"]
    assert "k_EResultAccountNotFound" in c["detail"] or "CustomAuthenticationFailed" in c["detail"]
    assert "App ID 1898300" in c["fix"] and c["fix"] in r["suggestions"] and r["overall"] == "fail"
    assert r["problem"]["id"] == "catalog-1" and r["checks"][1]["id"] == "problem"
    assert TOKEN not in json.dumps(r) and SECRET_PW not in json.dumps(r)


def test_check_setup_has_no_problem_row_when_nothing_matches_or_ready(svc, backend):
    start(svc, backend, logs="booting\n")
    r = svc.verify_game_setup("tok")
    assert "problem" not in by_id(r) and r["problem"] is None
    backend.set_logs("gamedock-tok", "session is open\nNo space left on device\n")
    assert "problem" not in by_id(svc.verify_game_setup("tok"))


def test_check_setup_explains_an_oom_killed_container(svc, backend):
    svc.deploy("tokgame", "Tok", env=ENV, start=True)
    svc.stop("tok")
    backend.exit_info["gamedock-tok"] = (137, True)
    r = svc.verify_game_setup("tok")
    assert by_id(r)["problem"]["status"] == "fail" and "ran out of memory" in by_id(r)["problem"]["detail"]
    backend.exit_info["gamedock-tok"] = (137, False)
    assert "killed" in by_id(svc.verify_game_setup("tok"))["problem"]["detail"]
    backend.exit_info["gamedock-tok"] = (0, False)
    assert "problem" not in by_id(svc.verify_game_setup("tok"))


def test_diagnostics_include_the_problem_and_the_log_file_tail_redacted(svc, backend):
    start(svc, backend)
    game_log(svc, FILE_LOG + f"auth token={TOKEN} pw={SECRET_PW}\nlast line\n")
    d = svc.get_server_diagnostics("tok")
    assert d["server"]["problem"]["id"] == "catalog-1"
    lf = d["log_files"][0]
    assert lf["path"] == "server/logs/game.log" and lf["found"] and "last line" in lf["text"]
    text = svc.diagnostics_text(d)
    assert "PROBLEM: Steam rejected the login token" in text and "Next step:" in text
    assert "end of the game's log file server/logs/game.log" in text
    for secret in (TOKEN, SECRET_PW):
        assert secret not in text and secret not in json.dumps(d)


def test_diagnostics_without_the_log_file_say_so(svc, backend):
    start(svc, backend)
    d = svc.get_server_diagnostics("tok")
    assert d["log_files"] == [{"path": "server/logs/game.log", "found": False, "text": ""}]
    assert "file not found" in svc.diagnostics_text(d)


# ---------------------------------------------------------------------------------------------- web and MCP
def test_web_api_carries_the_problem(svc, backend):
    start(svc, backend)
    game_log(svc)
    c = TestClient(create_app(svc))
    s = [x for x in c.get("/api/servers").json() if x["id"] == "tok"][0]
    assert s["problem"]["title"].startswith("Steam rejected") and s["problem"]["advice"]
    r = c.get("/api/servers/tok/check-setup").json()
    assert r["problem"]["id"] == "catalog-1"
    cat = [e for e in c.get("/api/catalog").json() if e["id"] == "tokgame"][0]
    assert cat["fail_log_patterns"][0]["title"] and cat["log_files"] == ["server/logs/game.log"]
    assert cat["fields"][1]["pattern"] == "[0-9A-Fa-f]{32}" and cat["builtin_fail_patterns"] is True


def test_mcp_tools_return_the_problem(svc, backend):
    start(svc, backend)
    game_log(svc)
    app = make_app(svc, mode="read_only")

    async def go(c):
        _, st = await call(c, "get_server_status", server_id="tok")
        _, ls = await call(c, "list_servers")
        _, vr = await call(c, "verify_game_setup", server_id="tok")
        _, dg = await call(c, "get_server_diagnostics", server_id="tok")
        return st, ls, vr, dg
    st, ls, vr, dg = run(app, go)
    assert st["problem"]["title"].startswith("Steam rejected")
    assert ls["servers"][0]["problem"]["id"] == "catalog-1"
    assert vr["problem"]["id"] == "catalog-1" and any(c["id"] == "problem" for c in vr["checks"])
    assert dg["server"]["problem"]["advice"] and dg["log_files"][0]["found"]
    assert TOKEN not in json.dumps([st, ls, vr, dg])


def test_the_guide_mentions_the_new_authoring_keys(svc):
    app = make_app(svc, mode="standard")

    async def go(c):
        return await call(c, "get_add_game_guide")
    err, g = run(app, go)
    text = json.dumps(g)
    assert not err and "fail_log_patterns" in text and "log_files" in text and "pattern_hint" in text


# ---------------------------------------------------------------------------------------------- the bundled ASKA example
ASKA = ROOT / "docs" / "catalog-entries" / "aska.yaml"


def test_aska_example_is_valid_strict_and_not_loaded_as_a_catalog_entry():
    c = au.check_text(ASKA.read_text())
    assert c.ok, c.errors
    e = c.entry
    assert e.steam_app_id == "3246670" and "1898300" in e.notes and "1898300" in {f.name: f for f in e.fields}["ASKA_AUTH_TOKEN"].description
    names = {f.name for f in e.fields}
    assert {"ASKA_MODE", "ASKA_SEED", "ASKA_KEEP_WORLD_ALIVE", "ASKA_AUTOSAVE_STYLE", "ASKA_SAVE_BACKUP", "ASKA_KEEP_SAVES", "TZ",
            "ASKA_DISPLAY_NAME", "ASKA_SERVER_NAME", "ASKA_AUTH_TOKEN", "ASKA_PASSWORD", "ASKA_REGION", "ASKA_MAX_PLAYERS",
            "ASKA_SAVE_ID"} <= names
    f = {x.name: x for x in e.fields}
    assert f["ASKA_SAVE_BACKUP"].default == "1" and f["ASKA_AUTH_TOKEN"].secret and f["ASKA_AUTH_TOKEN"].required
    assert f["ASKA_PASSWORD"].secret and "Web API" in f["ASKA_AUTH_TOKEN"].description
    assert e.log_files == ("server/logs/aska_game_output.log",) and e.ready_log_pattern == "The session is now open!"
    assert any("CustomAuthenticationFailed" in p.pattern and "1898300" in p.advice and "not a Steam" in p.advice
               for p in e.fail_log_patterns)
    import re
    tok = f["ASKA_AUTH_TOKEN"].pattern
    assert re.fullmatch(tok, "A" * 32) and not re.fullmatch(tok, "A" * 31) and not re.fullmatch(tok, "G" * 32)
    assert not (ROOT / "catalog" / "aska.yaml").exists()   # it stays an AI-added entry: see CHANGELOG 0.6.1
    assert "aska" not in load_catalog(ROOT / "catalog")
    # no secrets or odd keys in a public file
    assert "token:" not in ASKA.read_text().lower()


def test_aska_example_lints_without_warnings(tmp_path):
    (tmp_path / "aska.yaml").write_text(ASKA.read_text())
    rep = lint_catalog(tmp_path)
    assert rep.errors == [] and rep.warnings == [], rep.warnings


def test_aska_example_deploys_validates_and_explains_the_steam_error(backend, tmp_path, clock):
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "aska.yaml").write_text(ASKA.read_text())
    svc = GameDockService(backend, cat, tmp_path / "data", clock=clock)
    with pytest.raises(ValidationError, match="exactly 32"):
        svc.deploy("aska", "A", env={"ASKA_AUTH_TOKEN": "my-steam-web-api-key-pasted-by-mistake"})
    svc.deploy("aska", "A", env={"ASKA_AUTH_TOKEN": TOKEN}, start=True)
    backend.health["gamedock-a"] = "unhealthy"
    backend.set_logs("gamedock-a", "[entrypoint] starting\n")
    log = svc.data_dir / "a" / "server" / "logs" / "aska_game_output.log"
    log.parent.mkdir(parents=True)
    log.write_text("Failed to connect to Steam reason: k_EResultAccountNotFound\nDisconnected with reason: CustomAuthenticationFailed\n")
    s = svc.get_status("a")
    assert s.problem["title"] == "Steam rejected the login token (CustomAuthenticationFailed)."
    assert "dev/managegameservers" in s.problem["advice"] and "1898300" in s.problem["advice"]
    assert "aska_game_output.log" in s.problem["where"] and TOKEN not in json.dumps(s.problem)
