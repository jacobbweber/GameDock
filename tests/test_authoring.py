"""Adding games: the loader's underscore rule, the validation and safety rules, MCP authoring, the review gate,
the web review actions, image inspection (no pulling) and the docs staying in sync with the code."""
import dataclasses
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamedock import authoring as au
from gamedock import authoring_service
from gamedock.backends import DockerCliBackend, FakeBackend
from gamedock.catalog import (
    CONFIG_FILE_KEYS, ENTRY_KEYS, FAIL_PATTERN_KEYS, FIELD_KEYS, FORBIDDEN_KEYS, PORT_KEYS, VOLUME_KEYS, entry_to_dict, load_catalog,
    parse_definition,
)
from gamedock.errors import BackendError, CatalogError, ConflictError, NotFoundError, ValidationError
from gamedock.mcp_api import McpConfig
from gamedock.models import CatalogEntry
from gamedock.web.app import create_app
from tests.conftest import ADMIN
from tests.mcp_helpers import call, make_app, run

ROOT = Path(__file__).resolve().parent.parent

GOOD = textwrap.dedent("""\
    # added by a test
    id: aska-test
    name: Test Game Server
    image: example/test-game-server:1.2
    description: A made-up game for tests.
    notes: Made up for tests. Nothing here was verified.
    ports:
      - { protocol: udp, host: 27100, container: 27015 }
      - { protocol: tcp, host: 27101, container: 27016 }
    volumes:
      - { host: ./saves, container: /saves }
    env:
      MODE: "1"
    fields:
      - { name: SERVER_NAME, label: Server name, default: "Test", required: true }
      - { name: SERVER_PASSWORD, label: Password, secret: true }
    config_files:
      - { path: saves/settings.ini, label: Settings, format: ini, sensitive_keys: [AdminPassword] }
    ready_log_pattern: "Server is ready"
    progress_log_pattern: "progress: *([0-9.]+)"
    steam_app_id: 123456
    """)


def good(**changes):
    """GOOD with some top-level keys replaced (value None removes the key)."""
    import yaml
    d = yaml.safe_load(GOOD)
    for k, v in changes.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    return yaml.safe_dump(d, sort_keys=False)


def errors_of(text):
    return au.check_text(text).errors


def sha(path: Path) -> str:
    return au.sha256_bytes(path.read_bytes())


@pytest.fixture
def records(service):
    return lambda: json.loads((service.data_dir / au.REVIEW_FILENAME).read_text())


# ------------------------------------------------------------------ loader
def test_loader_skips_files_starting_with_underscore_or_dot(tmp_path):
    (tmp_path / "ok.yaml").write_text("id: ok\nname: Ok\nimage: busybox\n")
    (tmp_path / "_template.yaml").write_text("id: template\nname: T\nimage: busybox\n")
    (tmp_path / "_broken.yaml").write_text("this: [is not\n")
    (tmp_path / ".hidden.yaml").write_text("id: hidden\nname: H\nimage: busybox\n")
    problems = []
    assert set(load_catalog(tmp_path, errors=problems)) == {"ok"}
    assert problems == []
    assert set(load_catalog(tmp_path)) == {"ok"}  # also without the errors list (no exception from _broken)


def test_repo_catalog_ignores_template_and_still_loads_everything():
    defs = load_catalog(ROOT / "catalog")
    assert "replace-me" not in defs and {"palworld", "enshrouded"} <= set(defs)
    assert all(e.origin == "repo" and e.reviewed for e in defs.values())


def test_template_file_is_a_valid_entry():
    text = (ROOT / "catalog" / "_template.yaml").read_text()
    c = au.check_text(text)
    assert c.errors == [] and c.entry.id == "replace-me"
    assert "ASKA" not in text.upper()   # the template must not carry game specifics


def test_fallback_template_and_procedure_are_valid():
    assert au.check_text(authoring_service.FALLBACK_TEMPLATE).errors == []
    assert all(f"{i}." in authoring_service.FALLBACK_PROCEDURE for i in range(1, 11))


# ------------------------------------------------------------------ validation rules (all of them)
def test_good_entry_is_valid():
    c = au.check_text(GOOD)
    assert c.errors == [] and c.entry.origin == "mcp" and c.entry.steam_app_id == "123456"


@pytest.mark.parametrize("text,fragment", [
    ("", "empty"),
    ("   \n", "empty"),
    ("- just\n- a list\n", "top level"),
    ("id: [unclosed\n", "not valid YAML"),
    (good(image="has space"), "not a valid image name"),
    (good(image="a/../b"), "not a valid image name"),
    (good(image="--privileged"), "not a valid image name"),
    (good(image="registry//x"), "not a valid image name"),
    (good(volumes=[{"host": "/etc", "container": "/x"}]), "not relative"),
    (good(volumes=[{"host": "~/saves", "container": "/x"}]), "not relative"),
    (good(volumes=[{"host": "$HOME/saves", "container": "/x"}]), "not relative"),
    (good(volumes=[{"host": "C:\\games", "container": "/x"}]), "not relative"),
    (good(volumes=[{"host": "../outside", "container": "/x"}]), "'..'"),
    (good(volumes=[{"host": "./a", "container": "/var/run/docker.sock"}]), "Docker socket"),
    (good(volumes=[{"host": "/var/run/docker.sock", "container": "/x"}]), "Docker socket"),
    (good(volumes=[{"host": "./a", "container": "relative"}]), "absolute"),
    (good(privileged=True), "privileged mode"),
    (good(network_mode="host"), "network mode"),
    (good(devices=["/dev/dri:/dev/dri"]), "device mappings"),
    (good(cap_add=["SYS_ADMIN"]), "extra capabilities"),
    (good(security_opt=["seccomp=unconfined"]), "security option"),
    (good(pid="host"), "process space"),
    (good(ports=[{"protocol": "udp", "host": 80, "container": 80}]), "below 1024"),
    (good(ports=[{"protocol": "udp", "host": 1023, "container": 80}]), "below 1024"),
    (good(ports=[{"protocol": "udp", "host": 70000, "container": 80}]), "out of range"),
    (good(ports=[{"protocol": "udp", "host": 30000, "container": 0}]), "out of range"),
    (good(ports=[{"protocol": "icmp", "host": 30000, "container": 80}]), "tcp or udp"),
    (good(ports=[{"protocol": "sctp", "host": 30000, "container": 80}]), "tcp or udp"),
    (good(ports=[{"protocol": "udp", "host": 30000, "container": 80}, {"protocol": "udp", "host": 30000, "container": 81}]),
     "listed twice"),
    (good(ports="27015"), "must be a list"),
    (good(volumes=5), "must be a list"),
    (good(env={"bad name": "1"}), "bad name in 'env'"),
    (good(env={"PATH": "/x"}), "reserved"),
    (good(env={"A": "multi\nline"}), "line breaks"),
    (good(fields=[{"name": "1BAD"}]), "bad env field"),
    (good(fields=[{"name": "DOCKER_HOST"}]), "reserved"),
    (good(ready_log_pattern="(unclosed"), "regular expression"),
    (good(ready_log_pattern="a" * 301), "at most 300"),
    (good(progress_log_pattern="no group"), "exactly 1 capture group"),
    (good(progress_log_pattern="(a)(b)"), "exactly 1 capture group"),
    (good(version_log_pattern="[bad"), "regular expression"),
    (good(steam_app_id="abc"), "steam_app_id"),
    (good(update_method="sometimes"), "update_method"),
    (good(stop_timeout=9999), "stop_timeout"),
    (good(config_files=[{"path": "../x"}]), "relative"),
    (good(config_files=[{"path": "/abs"}]), "relative"),
    (good(config_files=[{"path": "a", "format": "xml"}]), "format"),
    (good(description="x" * 2001), "longer than"),
    (good(mystery_key="x"), "keys GameDock does not know: mystery_key"),
    (good(ports=[{"protocol": "udp", "host": 30000, "container": 80, "extra": 1}]), "'ports' item 1 has keys"),
    (good(fields=[{"name": "A", "bogus": 1}]), "'fields' item 1 has keys"),
    (good(id="_hidden"), "underscore"),
    (good(id="Has Capitals"), "id"),
    (good(id=None), "missing or invalid 'id'"),
    (good(image=None), "missing or invalid 'image'"),
])
def test_validation_rules(text, fragment):
    errs = errors_of(text)   # never raises, whatever the text
    assert errs, f"expected a problem mentioning {fragment!r}"
    assert any(fragment in e for e in errs), errs


def test_size_limit_is_20kb():
    big = GOOD + "# " + "x" * (au.MAX_YAML_BYTES) + "\n"
    errs = errors_of(big)
    assert errs and "larger than 20 KB" in errs[0]
    ok = GOOD + "# " + "x" * 5000 + "\n"
    assert errors_of(ok) == []


def test_yaml_tricks_are_refused():
    assert "anchors" in errors_of(GOOD + "x: &a [1]\ny: *a\n")[0]
    assert "written twice" in errors_of(GOOD + "name: Again\n")[0]
    assert "NUL" in errors_of(GOOD + "\0")[0]
    assert errors_of(GOOD.replace("id: aska-test", "!!python/object/apply:os.system ['x']\nid: aska-test"))  # never executes


def test_several_problems_are_all_reported():
    errs = errors_of(good(privileged=True, network_mode="host",
                          volumes=[{"host": "/etc", "container": "/x"}],
                          ports=[{"protocol": "udp", "host": 22, "container": 22}]))
    text = " ".join(errs)
    for part in ("privileged mode", "network mode", "not relative", "below 1024"):
        assert part in text


def test_dangerous_keys_are_refused_for_repo_entries_too():
    for key in FORBIDDEN_KEYS:
        with pytest.raises(CatalogError):
            parse_definition({"id": "g", "name": "G", "image": "busybox", key: True}, origin="repo")
    with pytest.raises(CatalogError, match="Docker socket"):
        parse_definition({"id": "g", "name": "G", "image": "busybox",
                          "volumes": [{"host": "./x", "container": "/run/docker.sock"}]}, origin="repo")


def test_repo_entries_may_use_absolute_folders_and_low_ports_but_ai_entries_may_not():
    data = {"id": "g", "name": "G", "image": "busybox", "mystery": 1,
            "ports": [{"protocol": "tcp", "host": 80, "container": 80}],
            "volumes": [{"host": "/srv/share/g", "container": "/data"}]}
    e = parse_definition(data, origin="repo")       # trusted: allowed (unknown keys are ignored as before)
    assert e.volumes[0].host == "/srv/share/g" and e.ports[0].host == 80
    with pytest.raises(CatalogError):
        parse_definition(data, origin="mcp")


def test_warnings_flag_unusual_things():
    risky = good(image="ghcr.io/someone/thing:latest", description="",
                 fields=[{"name": "ADMIN_PASSWORD", "label": "x"}, {"name": "TOKEN", "secret": True, "default": "abc"}],
                 env={"API_TOKEN": "hardcoded"}, volumes=[], notes="")
    c = au.check_text(risky)
    assert c.errors == []   # all of these are legal: they only produce warnings
    flags = [f.text for f in au.analyze_entry(c.entry)]
    joined = " ".join(flags)
    assert "ghcr.io" in joined and "no fixed version" in joined and "mounts no folders" in joined
    assert "API_TOKEN" in joined and "not marked secret" in joined and "default value" in joined
    assert "no description" in joined.lower() and "no notes" in joined.lower()


def test_summary_is_plain_and_complete():
    s = au.summarize_entry(au.check_text(GOOD).entry)
    text = " ".join(s["lines"])
    assert "example/test-game-server:1.2" in text and "27100 (UDP" in text and "./saves as /saves" in text
    assert s["secret_fields"] == ["SERVER_PASSWORD"] and s["folders"] == [{"host": "./saves", "container": "/saves"}]


# ------------------------------------------------------------------ add / update / refuse
def test_validate_saves_nothing(service):
    r = service.validate_catalog_entry(GOOD)
    assert r["valid"] and r["can_add"] and r["file_name"] == "aska-test.yaml" and r["summary"]["image"]
    assert not (service.catalog_dir / "aska-test.yaml").exists()
    assert not (service.data_dir / au.REVIEW_FILENAME).exists()


def test_validate_reports_existing_ids(service):
    r = service.validate_catalog_entry(good(id="sample"))
    assert r["valid"] and not r["can_add"] and r["exists"] and "repository" in r["next_step"]


def test_add_writes_file_and_unreviewed_record(service, records):
    r = service.add_catalog_entry(GOOD)
    path = service.catalog_dir / "aska-test.yaml"
    assert r["status"] == "added" and r["reviewed"] is False and r["can_deploy"] is False
    assert path.read_text().startswith("# added by a test") and path.read_text().endswith("\n")
    rec = records()["aska-test"]
    assert rec["added_by"] == "mcp" and rec["reviewed"] is False and rec["yaml_sha256"] == sha(path) == r["yaml_sha256"]
    assert rec["added_at"].endswith("Z")
    assert (service.data_dir / au.REVIEW_FILENAME).stat().st_mode & 0o777 == 0o600
    e = service.get_catalog_entry("aska-test")
    assert e.origin == "mcp" and e.reviewed is False and e.source_file == "aska-test.yaml"
    assert not [p for p in service.catalog_dir.iterdir() if p.name.endswith(".tmp")]   # atomic write left nothing behind


def test_add_refuses_invalid_entries_and_saves_nothing(service):
    with pytest.raises(ValidationError, match="NOT saved"):
        service.add_catalog_entry(good(privileged=True))
    assert not (service.catalog_dir / "aska-test.yaml").exists()
    assert not (service.data_dir / au.REVIEW_FILENAME).exists()


def test_add_never_overwrites(service, records):
    service.add_catalog_entry(GOOD)
    before = (service.catalog_dir / "aska-test.yaml").read_text()
    with pytest.raises(ConflictError, match="already exists"):
        service.add_catalog_entry(good(name="Other name"))
    assert (service.catalog_dir / "aska-test.yaml").read_text() == before
    sample_before = (service.catalog_dir / "sample.yaml").read_text()
    with pytest.raises(ConflictError, match="already exists"):
        service.add_catalog_entry(good(id="sample"))
    assert (service.catalog_dir / "sample.yaml").read_text() == sample_before
    assert set(records()) == {"aska-test"}


def test_add_refuses_when_another_file_type_has_the_id(service):
    (service.catalog_dir / "taken.json").write_text(json.dumps({"id": "something-else", "name": "x", "image": "busybox"}))
    with pytest.raises(ConflictError):
        service.add_catalog_entry(good(id="taken"))


@pytest.mark.parametrize("bad_id", ["../evil", "..", "a/b", "a\\b", "/abs", "_template", "UPPER", "", "x" * 80, "with space"])
def test_add_cannot_escape_the_catalog_folder(service, tmp_path, bad_id):
    with pytest.raises(ValidationError):
        service.add_catalog_entry(good(id=bad_id))
    assert sorted(p.name for p in service.catalog_dir.iterdir()) == ["sample.yaml"]
    assert not list(tmp_path.glob("evil*")) and not list(tmp_path.parent.glob("evil*"))


def test_file_for_guards_against_traversal(service):
    for bad in ("../x", "a/../b", "/etc/passwd", "_x"):
        with pytest.raises(ValidationError):
            service.authoring._file_for(bad)


def test_failed_write_removes_the_record(service, records, monkeypatch):
    from gamedock.errors import OperationError

    def boom(*a, **k):
        raise OperationError("disk full")

    monkeypatch.setattr(service.authoring, "_write_atomic", boom)
    with pytest.raises(OperationError, match="disk full"):
        service.add_catalog_entry(GOOD)
    assert "aska-test" not in records()          # no record left behind for a file that does not exist
    assert not (service.catalog_dir / "aska-test.yaml").exists()


def test_update_resets_review_and_hash(service, records):
    service.add_catalog_entry(GOOD)
    path = service.catalog_dir / "aska-test.yaml"
    h1 = records()["aska-test"]["yaml_sha256"]
    # editing an UNREVIEWED entry changes the recorded hash
    r = service.update_catalog_entry(good(description="A better description."))
    h2 = records()["aska-test"]["yaml_sha256"]
    assert h2 != h1 and h2 == sha(path) and r["status"] == "updated" and r["reviewed"] is False
    # approve, then update: back to unreviewed
    service.approve_catalog_entry("aska-test", h2)
    assert service.get_catalog_entry("aska-test").reviewed is True
    service.update_catalog_entry(good(description="Changed after review."))
    rec = records()["aska-test"]
    assert rec["reviewed"] is False and "reviewed_sha256" not in rec
    assert service.get_catalog_entry("aska-test").reviewed is False


def test_update_refuses_repo_entries_and_unknown_ids(service):
    before = (service.catalog_dir / "sample.yaml").read_text()
    with pytest.raises(ConflictError, match="came from the repository"):
        service.update_catalog_entry(good(id="sample", name="Hijacked"))
    assert (service.catalog_dir / "sample.yaml").read_text() == before
    with pytest.raises(NotFoundError):
        service.update_catalog_entry(GOOD)


def test_update_refuses_git_tracked_files(service):
    service.add_catalog_entry(GOOD)
    d = service.catalog_dir
    for cmd in (["init", "-q"], ["add", "aska-test.yaml"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(d), *cmd], check=True, capture_output=True)
    with pytest.raises(ConflictError, match="tracked by git"):
        service.update_catalog_entry(good(description="nope"))
    with pytest.raises(ConflictError, match="tracked by git"):
        service.reject_catalog_entry("aska-test", True)
    assert (d / "aska-test.yaml").exists()


# ------------------------------------------------------------------ the review gate
def test_unreviewed_entry_cannot_be_deployed_by_the_service(service, backend):
    service.add_catalog_entry(GOOD)
    with pytest.raises(ConflictError, match="has not been approved"):
        service.deploy("aska-test", "Test", env={})
    assert not [c for c in backend.calls if c[0] == "create"] and service.list_servers() == []


def test_api_deploy_is_refused_until_approved(service):
    client = TestClient(create_app(service))
    service.add_catalog_entry(GOOD)
    body = {"game_id": "aska-test", "name": "T", "env": {}}
    r = client.post("/api/servers", json=body)
    assert r.status_code == 409 and "approved" in r.json()["error"]
    entry = next(e for e in client.get("/api/catalog").json() if e["id"] == "aska-test")
    assert entry["origin"] == "mcp" and entry["reviewed"] is False and entry["deployable"] is False
    assert "needs your review" in entry["review_note"]
    rv = client.get("/api/catalog/aska-test/review").json()
    assert client.post("/api/catalog/aska-test/approve", json={"yaml_sha256": rv["yaml_sha256"]}).status_code == 200
    assert client.post("/api/servers", json=body).status_code == 201


def test_mcp_deploy_is_refused_until_approved(service):
    app = make_app(service, "standard")

    async def go(c):
        err, r = await call(c, "add_catalog_entry", yaml_text=GOOD)
        assert not err and r["status"] == "added" and r["can_deploy"] is False
        err, msg = await call(c, "deploy_server", game_id="aska-test", name="T")
        assert err and "not been approved" in msg
        err, cat = await call(c, "get_catalog_entry", game_id="aska-test")
        assert cat["reviewed"] is False and cat["origin"] == "mcp"
        return r["yaml_sha256"]

    h = run(app, go)
    service.approve_catalog_entry("aska-test", h)   # web only

    async def after(c):
        err, r = await call(c, "deploy_server", game_id="aska-test", name="T", env={})
        assert not err, r
        return r["id"]

    assert run(app, after) == "t"


def test_file_changed_after_approval_is_unreviewed_again(service):
    service.add_catalog_entry(GOOD)
    h = service.catalog_review("aska-test")["yaml_sha256"]
    service.approve_catalog_entry("aska-test", h)
    assert service.get_catalog_entry("aska-test").reviewed
    path = service.catalog_dir / "aska-test.yaml"
    path.write_text(path.read_text() + "# someone edited the file\n")
    e = service.get_catalog_entry("aska-test")
    assert e.reviewed is False
    info = service.catalog_review("aska-test")
    assert info["changed_after_review"] is True and info["can_approve"]
    with pytest.raises(ConflictError):
        service.deploy("aska-test", "x")


def test_approve_needs_the_fingerprint_that_was_shown(service):
    service.add_catalog_entry(GOOD)
    with pytest.raises(ConflictError, match="changed while you were looking"):
        service.approve_catalog_entry("aska-test", "0" * 64)
    with pytest.raises(ConflictError):
        service.approve_catalog_entry("aska-test", "")
    assert not service.get_catalog_entry("aska-test").reviewed


def test_approve_records_the_reviewed_hash(service, records):
    service.add_catalog_entry(GOOD)
    h = sha(service.catalog_dir / "aska-test.yaml")
    r = service.approve_catalog_entry("aska-test", h)
    rec = records()["aska-test"]
    assert r["reviewed"] and rec["reviewed"] is True and rec["reviewed_sha256"] == h and rec["reviewed_at"].endswith("Z")
    assert rec["reviewed_by"] == "web"
    assert service.approve_catalog_entry("aska-test", h)["note"].startswith("This entry was already")


def test_approve_and_reject_refuse_repo_entries(service):
    with pytest.raises(NotFoundError):
        service.approve_catalog_entry("sample", "x")
    with pytest.raises(NotFoundError):
        service.reject_catalog_entry("sample", True)
    assert (service.catalog_dir / "sample.yaml").exists()


def test_reject_needs_confirmation_then_deletes_file_and_record(service, records):
    service.add_catalog_entry(GOOD)
    with pytest.raises(ValidationError):
        service.reject_catalog_entry("aska-test", False)
    assert (service.catalog_dir / "aska-test.yaml").exists()
    r = service.reject_catalog_entry("aska-test", True)
    assert r["deleted"] and not (service.catalog_dir / "aska-test.yaml").exists()
    assert "aska-test" not in records()
    assert [e.id for e in service.list_catalog()] == ["sample"]


def test_web_review_endpoints_and_status_banner(service):
    client = TestClient(create_app(service))
    service.add_catalog_entry(GOOD)
    st = client.get("/api/status").json()
    assert st["catalog_review_pending"] == [{"id": "aska-test", "name": "Test Game Server"}]
    rv = client.get("/api/catalog/aska-test/review").json()
    assert rv["can_approve"] and "id: aska-test" in rv["yaml"]
    assert any("example/test-game-server:1.2" in line for line in rv["summary"]["lines"])
    assert client.post("/api/catalog/aska-test/reject").status_code == 400          # no confirm
    assert client.post("/api/catalog/aska-test/reject?confirm=true").status_code == 200
    assert client.get("/api/status").json()["catalog_review_pending"] == []
    assert client.post("/api/catalog/sample/approve", json={"yaml_sha256": "x"}).status_code == 404
    assert client.get("/api/catalog/nope/review").status_code == 404


def test_web_approval_is_audited(service, tmp_path):
    client = TestClient(create_app(service, mcp_config=McpConfig(mode="standard")))
    service.add_catalog_entry(GOOD)
    rv = client.get("/api/catalog/aska-test/review").json()
    client.post("/api/catalog/aska-test/approve", json={"yaml_sha256": rv["yaml_sha256"]})
    lines = [json.loads(x) for x in (tmp_path / "data" / "mcp-audit.jsonl").read_text().splitlines()]
    assert any(x["tool"] == "approve_catalog_entry" and x["client"] == "web" for x in lines)


def test_review_record_problem_is_reported(service):
    service.add_catalog_entry(GOOD)
    (service.data_dir / au.REVIEW_FILENAME).write_text("{ not json")
    service.list_catalog()
    assert any(f == au.REVIEW_FILENAME for f, _ in service.catalog_problems)
    with pytest.raises(Exception, match="not valid JSON"):
        service.add_catalog_entry(good(id="another"))   # never writes on top of a record it cannot read


# ------------------------------------------------------------------ MCP tools
def test_authoring_tools_per_mode(service):
    async def names(c):
        return {t.name for t in (await c.list_tools()).tools}

    ro = run(make_app(service, "read_only"), names)
    assert {"get_add_game_guide", "validate_catalog_entry", "inspect_image"} <= ro
    assert not {"add_catalog_entry", "update_catalog_entry"} & ro
    for mode in ("standard", "full"):
        both = run(make_app(service, mode), names)
        assert {"get_add_game_guide", "validate_catalog_entry", "inspect_image", "add_catalog_entry",
                "update_catalog_entry"} <= both
    # approving / rejecting is never an MCP tool
    full = run(make_app(service, "full"), names)
    assert not {n for n in full if "approve" in n or "reject" in n}
    # the only "review" tools only LOOK (0.6.0): get_catalog_review_status, and preview_deploy (it contains "review")
    assert {n for n in full if "review" in n} == {"get_catalog_review_status", "preview_deploy"}


def test_add_tool_is_not_callable_in_read_only_mode(service):
    async def go(c):
        return await call(c, "add_catalog_entry", yaml_text=GOOD)

    err, msg = run(make_app(service, "read_only"), go)
    assert err and "Unknown tool" in msg and not (service.catalog_dir / "aska-test.yaml").exists()


def test_guide_tool_and_resource(service):
    async def go(c):
        err, g = await call(c, "get_add_game_guide")
        assert not err
        assert "Step 1." in g["procedure_markdown"] and len(g["steps"]) == 10
        assert {r["key"] for r in g["field_reference"]} >= {"id", "image", "ports[].host", "steam_app_id"}
        assert "REPLACE" in g["template_yaml"] and g["limits"]["max_yaml_bytes"] == 20480
        res = (await c.read_resource("gamedock://add-a-game")).contents[0].text
        assert "Adding a game to GameDock" in res
        guide = (await c.read_resource("gamedock://guide")).contents[0].text
        for word in ("add_catalog_entry", "validate_catalog_entry", "inspect_image", "needs your review", "gamedock://add-a-game"):
            assert word in guide
        p = await c.get_prompt("add_a_game", {"game_name": "Some Game"})
        assert "Some Game" in p.messages[0].content.text and "NEVER invent" in p.messages[0].content.text
        return True

    assert run(make_app(service, "full"), go)


def test_read_only_guide_does_not_promise_add_tools(service):
    async def go(c):
        p = await c.get_prompt("add_a_game", {"game_name": "X"})
        return p.messages[0].content.text

    assert "read-only" in run(make_app(service, "read_only"), go)


def test_guide_falls_back_when_docs_are_missing(service, monkeypatch, tmp_path):
    monkeypatch.setattr(authoring_service, "PROCEDURE_PATH", tmp_path / "missing.md")
    monkeypatch.setattr(authoring_service, "REPO_ROOT", tmp_path)
    g = service.add_game_guide()
    assert "short version" in g["procedure_markdown"] and "id: my-game" in g["template_yaml"]


def test_template_is_read_from_the_catalog_folder_first(service):
    (service.catalog_dir / "_template.yaml").write_text("# custom template\nid: x\n")
    assert service.add_game_guide()["template_yaml"].startswith("# custom template")


def test_mcp_authoring_flow_and_audit(service, tmp_path):
    app = make_app(service, "standard")

    async def go(c):
        err, v = await call(c, "validate_catalog_entry", yaml_text=good(privileged=True))
        assert not err and v["valid"] is False and any("privileged" in e for e in v["errors"])
        err, v = await call(c, "validate_catalog_entry", yaml_text=GOOD)
        assert v["valid"] and v["can_add"] and not (service.catalog_dir / "aska-test.yaml").exists()
        err, bad = await call(c, "add_catalog_entry", yaml_text=good(network_mode="host"))
        assert err and "NOT saved" in bad
        err, r = await call(c, "add_catalog_entry", yaml_text=GOOD)
        assert not err and r["status"] == "added" and r["reviewed"] is False
        err, again = await call(c, "add_catalog_entry", yaml_text=GOOD)
        assert err and "already exists" in again
        err, up = await call(c, "update_catalog_entry", yaml_text=good(description="v2"))
        assert not err and up["status"] == "updated" and up["reviewed"] is False
        err, nope = await call(c, "update_catalog_entry", yaml_text=good(id="sample"))
        assert err and "repository" in nope
        err, cat = await call(c, "list_catalog")
        assert {g["id"]: g["reviewed"] for g in cat["games"]} == {"sample": True, "aska-test": False}
        return True

    assert run(app, go)
    audit = (tmp_path / "data" / "mcp-audit.jsonl").read_text()
    lines = [json.loads(x) for x in audit.splitlines()]
    tools = [x["tool"] for x in lines]
    for t in ("validate_catalog_entry", "add_catalog_entry", "update_catalog_entry"):
        assert t in tools
    assert "Test Game Server" not in audit and "example/test-game-server" not in audit   # YAML text is never logged
    assert any(x["tool"] == "add_catalog_entry" and "reviewed=false" in x.get("detail", "") and "added id=aska-test" in x["detail"]
               for x in lines)
    assert any(x["tool"] == "add_catalog_entry" and x["status"] == "error" for x in lines)   # refusals are audited too


def test_oversized_text_is_rejected_over_mcp(service):
    async def go(c):
        return await call(c, "add_catalog_entry", yaml_text=GOOD + "# " + "y" * 30_000)

    err, msg = run(make_app(service, "standard"), go)
    assert err and "20 KB" in msg


# ------------------------------------------------------------------ inspect_image: read-only, never pulls
def test_inspect_image_local_with_fake_backend(service, backend):
    backend.local_images["example/game:1"] = "sha256:abc"
    backend.image_facts["example/game:1"] = {"exposed_ports": ["27015/udp"], "volumes": ["/saves"], "user": "1000",
                                             "env_names": ["SERVER_NAME"], "healthcheck": None}
    backend.remote_facts["example/game:1"] = {"digest": "sha256:" + "a" * 64, "platforms": ["linux/amd64"]}
    r = service.inspect_image("example/game:1")
    assert r["local"] and r["local_details"]["exposed_ports"] == ["27015/udp"] and r["pulled_or_ran"] is False
    assert any("27015/udp" in s for s in r["summary"]) and any("linux/amd64" in s for s in r["summary"])
    assert ("inspect_image", "example/game:1") in backend.calls
    assert not [c for c in backend.calls if c[0] in ("pull", "create", "start")]


def test_inspect_image_not_local_says_how_to_pull(service, backend):
    backend.remote_facts["example/game:2"] = {"digest": "sha256:" + "b" * 64, "platforms": ["linux/arm64"]}
    r = service.inspect_image("example/game:2")
    text = " ".join(r["summary"])
    assert r["local"] is False and r["local_details"] is None
    assert "NOT stored on this machine" in text and "docker pull example/game:2" in text
    assert "no linux/amd64" in text
    assert not [c for c in backend.calls if c[0] in ("pull", "create")]
    assert "example/game:2" not in backend.local_images


def test_inspect_image_unknown_image_and_offline(service, backend):
    assert "not found" in " ".join(service.inspect_image("nobody/nothing:1")["summary"])
    backend.offline = True
    assert "no internet" in " ".join(service.inspect_image("nobody/nothing:1")["summary"])


@pytest.mark.parametrize("bad", ["", "a b", "--help", "a/../b", "x" * 300, "bad;rm", "a//b"])
def test_inspect_image_refuses_odd_references(service, bad):
    with pytest.raises(ValidationError):
        service.inspect_image(bad)


class Recorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        rc, out, err = self.results.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, err)


LOCAL_JSON = json.dumps([{
    "Id": "sha256:" + "c" * 64, "RepoDigests": ["x/y@sha256:" + "d" * 64], "RepoTags": ["x/y:1"], "Os": "linux",
    "Architecture": "amd64", "Size": 123456, "Created": "2026-01-01T00:00:00Z",
    "Config": {"User": "steam", "ExposedPorts": {"8211/udp": {}, "27015/tcp": {}, "27015/udp": {}},
               "Volumes": {"/data": {}}, "Env": ["PATH=/usr/bin", "SERVER_NAME=secretish", "PUID=1000"],
               "Healthcheck": {"Test": ["CMD", "true"], "Interval": 30_000_000_000}, "Entrypoint": ["/start.sh"],
               "WorkingDir": "/home/steam"}}])
IMAGETOOLS = ("Name:      docker.io/x/y:1\nMediaType: application/vnd.oci.image.index.v1+json\n"
              "Digest:    sha256:" + "e" * 64 + "\n\nManifests:\n  Name: a\n  Platform:    linux/amd64\n"
              "  Name: b\n  Platform:    linux/arm64\n  Name: c\n  Platform:    unknown/unknown\n")


def test_docker_inspect_local_image_reads_facts_without_values():
    rec = Recorder([(0, LOCAL_JSON, ""), (0, IMAGETOOLS, "")])
    r = DockerCliBackend(runner=rec).inspect_image("x/y:1")
    d = r["local_details"]
    assert r["local"] and d["exposed_ports"] == ["8211/udp", "27015/tcp", "27015/udp"] and d["volumes"] == ["/data"]
    assert d["user"] == "steam" and d["env_names"] == ["PATH", "SERVER_NAME", "PUID"]
    assert "secretish" not in json.dumps(r)   # environment VALUES are never returned
    assert d["healthcheck"]["interval_seconds"] == 30.0 and r["remote"]["platforms"] == ["linux/amd64", "linux/arm64"]


def test_docker_inspect_never_pulls_runs_or_creates():
    rec = Recorder([(1, "", "Error: No such image: x/y:1"), (1, "", "unsupported"), (0, json.dumps(
        {"manifests": [{"digest": "sha256:" + "f" * 64, "platform": {"os": "linux", "architecture": "amd64"}}]}), "")])
    r = DockerCliBackend(runner=rec).inspect_image("x/y:1")
    assert r["local"] is False and r["local_details"] is None and r["remote"]["platforms"] == ["linux/amd64"]
    for argv in rec.calls:
        assert argv[0] == "docker"
        assert argv[1] not in ("pull", "run", "create", "start", "exec", "build", "login", "push")
        assert argv[1:3] in (["image", "inspect"], ["buildx", "imagetools"], ["manifest", "inspect"])
    assert rec.calls[0][-2:] == ["--", "x/y:1"]   # the image can never be read as an option


def test_docker_inspect_registry_failure_is_plain():
    rec = Recorder([(1, "", "Error: No such image"), (1, "", "failed to resolve"), (1, "", "no such manifest: x/y:9")])
    r = DockerCliBackend(runner=rec).inspect_image("x/y:9")
    assert r["remote"] is None and "registry could not be asked" in r["remote_error"]


def test_docker_inspect_output_is_bounded():
    big = json.loads(LOCAL_JSON)
    big[0]["Config"]["ExposedPorts"] = {f"{p}/tcp": {} for p in range(1000, 1400)}
    big[0]["Config"]["Env"] = [f"VAR_{i}=x" for i in range(1000)]
    rec = Recorder([(0, json.dumps(big), ""), (0, IMAGETOOLS, "")])
    d = DockerCliBackend(runner=rec).inspect_image("x/y:1")["local_details"]
    assert len(d["exposed_ports"]) <= 60 and len(d["env_names"]) <= 120


def test_docker_inspect_rejects_bad_names_and_reports_docker_down():
    with pytest.raises(BackendError):
        DockerCliBackend(runner=Recorder([])).inspect_image("--evil")
    rec = Recorder([(1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?")])
    with pytest.raises(BackendError):
        DockerCliBackend(runner=rec).inspect_image("x/y:1")


# ------------------------------------------------------------------ docs and code stay in sync
def test_field_reference_names_exactly_the_keys_the_loader_knows():
    keys = au.reference_keys()
    assert keys[""] == set(ENTRY_KEYS)
    assert keys["ports"] == set(PORT_KEYS) and keys["volumes"] == set(VOLUME_KEYS)
    assert keys["fields"] == set(FIELD_KEYS) and keys["config_files"] == set(CONFIG_FILE_KEYS)
    assert keys["fail_log_patterns"] == set(FAIL_PATTERN_KEYS)
    assert set(keys) == {"", "ports", "volumes", "fields", "config_files", "fail_log_patterns"}


def test_every_model_attribute_and_serialised_key_is_a_catalog_key():
    metadata = {"origin", "reviewed", "source_file", "file_sha256"}
    assert {f.name for f in dataclasses.fields(CatalogEntry)} - metadata == set(ENTRY_KEYS)
    e = load_catalog(ROOT / "catalog")["palworld"]
    assert set(entry_to_dict(e)) == set(ENTRY_KEYS)
    d = entry_to_dict(e)
    assert set(d["ports"][0]) == set(PORT_KEYS) and set(d["volumes"][0]) == set(VOLUME_KEYS)
    assert set(d["fields"][0]) == set(FIELD_KEYS) and set(d["config_files"][0]) == set(CONFIG_FILE_KEYS)


def test_every_key_used_by_the_loader_source_is_in_the_reference():
    """Catch a key added to catalog.py (data.get("...")) without a reference row."""
    src = (ROOT / "gamedock" / "catalog.py").read_text()
    used = set(re.findall(r'data\.get\("([a-z_]+)"', src)) | set(re.findall(r'data\["([a-z_]+)"\]', src))
    assert used <= set(ENTRY_KEYS), used - set(ENTRY_KEYS)


def test_every_reference_row_is_complete():
    for r in au.FIELD_REFERENCE:
        assert set(r) == {"key", "type", "required", "default", "meaning"} and all(str(r[k]).strip() for k in r)


def test_doc_table_equals_generated_table():
    doc = (ROOT / "docs" / "ADDING_A_GAME.md").read_text()
    inner = doc.split("<!-- field-reference:start -->\n", 1)[1].split("\n<!-- field-reference:end -->", 1)[0]
    assert inner == au.field_reference_markdown(), "run: .venv/bin/python scripts/update_docs.py"
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "update_docs.py"), "--check"], capture_output=True)
    assert out.returncode == 0


def test_doc_has_the_required_sections():
    doc = (ROOT / "docs" / "ADDING_A_GAME.md").read_text()
    assert len(re.findall(r"^### Step \d+\.", doc, re.M)) == 10
    for key in (r["key"] for r in au.FIELD_REFERENCE):
        assert f"`{key}`" in doc
    for needle in ("What a catalog entry is", "Common traps", "Verification checklist", "Adding a game through MCP",
                   "_template.yaml", "PUID", "UDP", "empty password", "install folder", "regenerated", "docker.sock"):
        assert needle.lower() in doc.lower(), needle
    assert (ROOT / "docs" / "PROMPT_ADD_A_GAME.md").exists()


def test_prompt_doc_has_connection_details_and_no_token():
    p = (ROOT / "docs" / "PROMPT_ADD_A_GAME.md").read_text()
    assert "http://192.168.1.29:8100/mcp" in p and "Authorization: Bearer" in p and "<<GAME NAME>>" in p
    assert "NEVER print" in p and "FULL mode" in p
    assert not re.search(r"GAMEDOCK_MCP_TOKEN=\S{12,}", p)
    assert "aska" not in p.lower()   # game specifics stay a placeholder
