import json
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from gamedock import updates as up
from gamedock.backends import FakeBackend
from gamedock.errors import BusyError, ConflictError
from gamedock.service import GameDockService
from gamedock.web.app import create_app
from tests.conftest import SAMPLE
from tests.gitrepo import Repos, git


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Github:
    """Fake api.github.com for one repository."""

    def __init__(self, repos: Repos):
        self.repos = repos
        self.calls: list[str] = []
        self.down = False
        self.status_override: dict[str, tuple[int, str]] = {}
        self.compare = None   # (status, ahead_by) override

    def __call__(self, url, headers, timeout):
        assert url.startswith("https://api.github.com/repos/jacobbweber/GameDock")
        assert "User-Agent" in headers
        path = url.split("/repos/jacobbweber/GameDock", 1)[1]
        self.calls.append(path)
        if self.down:
            raise OSError("no route to host")
        for k, v in self.status_override.items():
            if path.startswith(k):
                return v
        sha = self.repos.head("remote")
        if path.startswith("/commits/"):
            return 200, json.dumps({"sha": sha, "commit": {"message": "Fix things\n\nlong", "committer": {"date": "2026-10-18T01:00:00Z"}}})
        if path.startswith("/contents/gamedock/__init__.py"):
            return 200, (self.repos.seed / "gamedock" / "__init__.py").read_text()
        if path.startswith("/compare/"):
            mine = path.split("/compare/")[1].split("...")[0]
            if self.compare:
                return 200, json.dumps({"status": self.compare[0], "ahead_by": self.compare[1]})
            known = subprocess.run(["git", "cat-file", "-e", mine], cwd=str(self.repos.seed), capture_output=True).returncode == 0
            if not known:
                return 404, "{}"
            rc = subprocess.run(["git", "merge-base", "--is-ancestor", mine, sha], cwd=str(self.repos.seed)).returncode
            return 200, json.dumps({"status": "ahead" if rc == 0 else "diverged", "ahead_by": 1})
        raise AssertionError(path)


@pytest.fixture
def repos(tmp_path):
    return Repos(tmp_path)


@pytest.fixture
def gh(repos):
    return Github(repos)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def restarts():
    return []


@pytest.fixture
def checker(repos, gh, clock, restarts):
    return up.UpdateChecker(install_dir=repos.local, http_get=gh, clock=clock, version="0.3.0",
                            restart=lambda: restarts.append(1))


# ------------------------------------------------------------------ parse_repo
@pytest.mark.parametrize("value", ["jacobbweber/GameDock", "https://github.com/jacobbweber/GameDock.git",
                                   "https://github.com/jacobbweber/GameDock/", "https://api.github.com/repos/jacobbweber/GameDock"])
def test_parse_repo_forms(value):
    assert up.parse_repo(value) == "jacobbweber/GameDock"


@pytest.mark.parametrize("bad", ["", "nothing", "a/b/c", "https://evil.example/a/b", "a/b;rm", "../x/y", "a b/c"])
def test_parse_repo_rejects(bad):
    with pytest.raises(ValueError):
        up.parse_repo(bad)


# ------------------------------------------------------------------ checking
def test_identical_means_no_update(checker, repos, gh):
    s = checker.check(force=True)
    assert s["update_available"] is False and s["latest"]["relation"] == "identical"
    assert s["commit"] == repos.head() and s["commit_short"] == repos.head()[:7]
    assert not any(c.startswith("/compare/") for c in gh.calls)


def test_newer_commit_is_an_update_with_version_and_message(checker, repos):
    repos.remote_commit(version="0.3.1")
    s = checker.check(force=True)
    assert s["update_available"] is True
    assert s["latest"]["version"] == "0.3.1" and s["latest"]["message"] == "Fix things"
    assert s["latest"]["ahead_by"] == 1 and s["latest"]["relation"] == "ahead"


def test_local_ahead_of_github_is_not_an_update(checker, repos, gh):
    repos.local_commit()
    s = checker.check(force=True)
    assert s["update_available"] is False and s["latest"]["relation"] == "unknown_local"   # GitHub never saw it


def test_behind_status_is_not_an_update(checker, repos, gh, clock):
    repos.remote_commit()
    gh.compare = ("behind", 0)
    assert checker.check(force=True)["update_available"] is False


def test_diverged_is_reported_as_update_but_cannot_apply(checker, repos, gh):
    repos.local_commit()
    repos.remote_commit()
    gh.compare = ("diverged", 1)
    assert checker.check(force=True)["update_available"] is True


def test_offline_is_a_note_not_an_error(checker, gh):
    gh.down = True
    s = checker.check(force=True)
    assert s["update_available"] is None and "works fine" in s["check_error"] and s["latest"] is None


def test_error_keeps_previous_good_answer_visible_state(checker, repos, gh, clock):
    repos.remote_commit()
    assert checker.check(force=True)["update_available"] is True
    clock.t += 100
    gh.down = True
    s = checker.check(force=True)
    assert s["check_error"] and s["latest"] is not None     # last good answer is still there


def test_rate_limited_and_unknown_repo_messages(checker, gh, clock):
    gh.status_override["/commits/"] = (403, "{}")
    assert "limiting" in checker.check(force=True)["check_error"]
    clock.t += 100
    gh.status_override["/commits/"] = (404, "{}")
    assert "private" in checker.check(force=True)["check_error"]


def test_at_most_every_six_hours_and_retry_after_failure(checker, gh, clock):
    checker.check()
    n = len(gh.calls)
    clock.t += 5 * 3600
    checker.check()
    assert len(gh.calls) == n and not checker.due()
    clock.t += 3600 + 1
    assert checker.due()
    checker.check()
    assert len(gh.calls) > n
    gh.down = True
    clock.t += 7 * 3600
    checker.check()
    assert checker.due() is False
    clock.t += 31 * 60
    assert checker.due() is True          # after a failure it retries after 30 minutes


def test_check_now_has_a_short_gap(checker, gh, clock):
    checker.check(force=True)
    n = len(gh.calls)
    checker.check(force=True)
    assert len(gh.calls) == n             # twice within 20 seconds: no second request
    clock.t += 21
    checker.check(force=True)
    assert len(gh.calls) > n


def test_disabled_never_contacts_github(repos, gh, clock):
    c = up.UpdateChecker(install_dir=repos.local, enabled=False, http_get=gh, clock=clock)
    assert c.check(force=True)["update_available"] is None and c.due() is False
    assert gh.calls == []
    assert c.can_self_update()[0] is False


def test_not_a_git_checkout_compares_version_numbers(tmp_path, gh, repos):
    plain = tmp_path / "plain"
    plain.mkdir()
    repos.remote_commit(version="0.4.0")
    c = up.UpdateChecker(install_dir=plain, http_get=gh, version="0.3.0")
    s = c.check(force=True)
    assert s["is_git"] is False and s["update_available"] is True
    ok, why = c.can_self_update()
    assert not ok and "not running from a git checkout" in why
    assert c.status()["self_update"]["possible"] is False


# ------------------------------------------------------------------ self-update safety
@pytest.fixture
def svc(tmp_path, checker):
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "s.yaml").write_text(SAMPLE)
    return GameDockService(FakeBackend(), cat, tmp_path / "data", updater=checker)


def apply(svc, checker):
    return checker.apply(svc.freeze, svc.unfreeze)


def test_apply_updates_and_restarts_once(svc, checker, repos, restarts):
    new = repos.remote_commit(version="0.3.1")
    out = apply(svc, checker)
    assert out["updated"] and out["restarting"] and out["version"] == "0.3.1"
    assert repos.head() == new
    assert svc._frozen   # stays frozen: GameDock is about to restart
    import time
    for _ in range(40):
        if restarts:
            break
        time.sleep(0.1)
    assert restarts == [1]
    with pytest.raises(BusyError):
        svc.deploy("sample", "X", env={"ADMIN_PASSWORD": "pw"})


def test_apply_when_already_current(svc, checker, restarts):
    out = apply(svc, checker)
    assert out["updated"] is False and "already up to date" in out["message"]
    assert not svc._frozen and restarts == []


def test_refuses_dirty_tree_and_shows_commands(svc, checker, repos):
    repos.remote_commit()
    (repos.local / "marker.txt").write_text("x")
    git(repos.local, "add", "marker.txt")   # a tracked file with local edits
    (repos.local / "pyproject.toml").write_text("changed locally")
    before = repos.head()
    with pytest.raises(ConflictError) as e:
        apply(svc, checker)
    assert "local changes" in str(e.value) and "git pull --ff-only" in str(e.value)
    assert repos.head() == before and not svc._frozen


def test_untracked_files_do_not_block(svc, checker, repos):
    repos.remote_commit()
    (repos.local / "catalog.custom.yaml").write_text("mine")   # a user's own file, not tracked
    assert apply(svc, checker)["updated"]


def test_refuses_while_an_action_is_in_progress(svc, checker, repos):
    svc.deploy("sample", "One", env={"ADMIN_PASSWORD": "pw"})
    repos.remote_commit()
    before = repos.head()
    with svc._operation("one", "backing up"):
        with pytest.raises(ConflictError) as e:
            apply(svc, checker)
    assert "busy" in str(e.value) and "one" in str(e.value)
    assert repos.head() == before and not svc._frozen


def test_refuses_while_scheduled_job_runs(svc, checker, repos):
    svc.deploy("sample", "One", env={"ADMIN_PASSWORD": "pw"})
    repos.remote_commit()
    svc._jobs_running.add(("one", "maintenance"))
    with pytest.raises(ConflictError, match="scheduled maintenance"):
        apply(svc, checker)


def test_refuses_when_local_commits_exist(svc, checker, repos):
    repos.local_commit()
    repos.remote_commit()
    before = repos.head()
    with pytest.raises(ConflictError, match="not on GitHub"):
        apply(svc, checker)
    assert repos.head() == before and not svc._frozen


def test_refuses_when_required_packages_change(svc, checker, repos):
    repos.remote_commit(files={"requirements.txt": "newdep\n"})
    before = repos.head()
    with pytest.raises(ConflictError) as e:
        apply(svc, checker)
    assert "required packages" in str(e.value) and "pip install -e ." in str(e.value)
    assert repos.head() == before and not svc._frozen


def test_version_only_pyproject_bump_does_not_block(svc, checker, repos):
    repos.remote_commit(files={"pyproject.toml": "[project]\nname='x'\nversion = '0.4.0'\n"})
    r = apply(svc, checker)
    assert r["updated"] and repos.head() == repos.head("seed")


def test_pyproject_dependency_change_still_blocks(svc, checker, repos):
    repos.remote_commit(files={"pyproject.toml": "[project]\nname='x'\nversion = '0.4.0'\ndependencies = ['newdep']\n"})
    before = repos.head()
    with pytest.raises(ConflictError, match="required packages"):
        apply(svc, checker)
    assert repos.head() == before and not svc._frozen


def test_refuses_on_detached_head(svc, checker, repos):
    git(repos.local, "checkout", "-q", "--detach")
    checker._git_cache = None
    with pytest.raises(ConflictError, match="detached"):
        apply(svc, checker)


def test_switched_off_by_setting(repos, gh):
    c = up.UpdateChecker(install_dir=repos.local, http_get=gh, self_update=False)
    ok, why = c.can_self_update()
    assert not ok and "GAMEDOCK_SELF_UPDATE" in why


def test_fetch_failure_changes_nothing(svc, checker, repos, tmp_path):
    git(repos.local, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    with pytest.raises(Exception, match="git fetch failed"):
        apply(svc, checker)
    assert not svc._frozen


def test_second_update_while_one_is_running_is_refused(svc, checker):
    assert checker._apply_lock.acquire(blocking=False)
    try:
        with pytest.raises(ConflictError, match="already in progress"):
            apply(svc, checker)
    finally:
        checker._apply_lock.release()


# ------------------------------------------------------------------ web
@pytest.fixture
def client(svc):
    with TestClient(create_app(svc)) as c:
        yield c


def test_version_endpoint_and_health(client, repos):
    v = client.get("/api/version").json()
    assert v["version"] == "0.3.0" and v["commit_short"] == repos.head()[:7] and v["self_update"]["possible"] is True
    assert client.get("/api/health").json() == {"ok": True, "version": "0.6.1"}
    assert "update_available" in client.get("/api/status").json()


def test_check_endpoint(client, repos):
    repos.remote_commit(version="0.3.2")
    r = client.post("/api/gamedock/update-check").json()
    assert r["update_available"] is True and r["latest"]["version"] == "0.3.2"
    assert client.get("/api/version").json()["update_available"] is True


def test_update_endpoint_needs_confirmation_and_never_restarts_unconfirmed(client, repos, restarts):
    repos.remote_commit()
    r = client.post("/api/gamedock/update")
    assert r.status_code == 400 and "confirmation" in r.json()["error"]
    assert restarts == []


def test_update_endpoint_refuses_outside_git(tmp_path):
    cat = tmp_path / "c"
    cat.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    s = GameDockService(FakeBackend(), cat, tmp_path / "d", updater=up.UpdateChecker(install_dir=plain))
    with TestClient(create_app(s)) as c:
        r = c.post("/api/gamedock/update?confirm=true")
    assert r.status_code == 409 and "not running from a git checkout" in r.json()["error"] and "git pull" in r.json()["error"]


def test_update_is_not_an_mcp_tool():
    from tests.test_mcp import DESTRUCTIVE, READ, STANDARD
    assert not any("update_gamedock" in n or "self_update" in n for n in READ | STANDARD | DESTRUCTIVE)


# ------------------------------------------------------------------ an untracked catalog file in the way of the update (0.6.1)
ASKA_V1 = "id: aska\nname: ASKA (as saved by an AI agent)\nimage: example/aska:1\n"
ASKA_V2 = "id: aska\nname: ASKA (bundled)\nimage: example/aska:2\n"


def bundle_aska(repos, text=ASKA_V2):
    (repos.seed / "catalog").mkdir(exist_ok=True)
    return repos.remote_commit(version="0.3.1", files={"catalog/aska.yaml": text})


def local_aska(repos, text=ASKA_V1):
    (repos.local / "catalog").mkdir(exist_ok=True)
    p = repos.local / "catalog" / "aska.yaml"
    p.write_text(text)
    return p


def test_plain_pull_would_have_failed_on_the_untracked_file(repos):
    """The reason for the set-aside step: git itself refuses (and so did GameDock 0.6.0's updater)."""
    local_aska(repos)
    bundle_aska(repos)
    import subprocess as sp
    r = sp.run(["git", "pull", "--ff-only", "origin", "main"], cwd=str(repos.local), capture_output=True, text=True)
    assert r.returncode != 0 and "untracked working tree files would be overwritten" in r.stderr


def test_a_different_untracked_catalog_file_is_set_aside_and_kept(svc, checker, repos, restarts, tmp_path):
    p = local_aska(repos)
    new = bundle_aska(repos)
    out = apply(svc, checker)
    assert out["updated"] and repos.head() == new
    assert p.read_text() == ASKA_V2                                   # the file that ships with GameDock is in place
    kept = out["catalog_files_replaced"]
    assert len(kept) == 1 and kept[0]["file"] == "catalog/aska.yaml" and kept[0]["identical"] is False
    assert Path(kept[0]["kept_at"]).read_text() == ASKA_V1
    assert str(repos.local / "data" / "catalog-displaced") in kept[0]["kept_at"] or "catalog-displaced" in kept[0]["kept_at"]
    assert kept[0]["kept_at"] in out["message"] and "approved again" in out["message"]
    assert git(repos.local, "status", "--porcelain", "--untracked-files=no") == ""   # still a clean checkout


def test_an_identical_untracked_file_is_simply_replaced(svc, checker, repos):
    p = local_aska(repos, ASKA_V2)
    bundle_aska(repos)
    out = apply(svc, checker)
    assert out["updated"] and out["catalog_files_replaced"][0]["identical"] is True and out["catalog_files_replaced"][0]["kept_at"] is None
    assert p.read_text() == ASKA_V2 and "approved again" not in out["message"]
    displaced = repos.local / "data" / "catalog-displaced"
    assert not displaced.exists() or list(displaced.iterdir()) == []   # no clutter left behind


def test_a_failed_pull_puts_the_file_back_exactly(svc, repos, gh, clock, restarts):
    p = local_aska(repos)
    bundle_aska(repos)

    def runner(args, cwd, timeout):
        if args[:2] == ["git", "pull"]:
            return subprocess.CompletedProcess(args, 1, "", "fatal: simulated failure")
        return up.default_runner(args, cwd, timeout)

    checker = up.UpdateChecker(install_dir=repos.local, http_get=gh, clock=clock, version="0.3.0", runner=runner,
                               restart=lambda: restarts.append(1))
    before = repos.head()
    from gamedock.errors import BackendError
    with pytest.raises(BackendError, match="simulated failure"):
        checker.apply(svc.freeze, svc.unfreeze)
    assert p.read_text() == ASKA_V1 and repos.head() == before and not svc._frozen and restarts == []
    displaced = repos.local / "data" / "catalog-displaced"
    assert not displaced.exists() or list(displaced.iterdir()) == []


def test_only_files_directly_in_catalog_are_ever_moved(svc, checker, repos):
    (repos.local / "docs").mkdir()
    mine = repos.local / "docs" / "notes.md"
    mine.write_text("my notes")
    (repos.seed / "docs").mkdir()
    repos.remote_commit(version="0.3.1", files={"docs/notes.md": "from GameDock"})
    with pytest.raises(Exception, match="untracked working tree files would be overwritten|pull --ff-only failed"):
        apply(svc, checker)
    assert mine.read_text() == "my notes" and not svc._frozen


def test_a_symbolic_link_in_the_way_is_left_alone(svc, checker, repos, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text(ASKA_V1)
    (repos.local / "catalog").mkdir()
    (repos.local / "catalog" / "aska.yaml").symlink_to(target)
    bundle_aska(repos)
    with pytest.raises(Exception):
        apply(svc, checker)
    assert (repos.local / "catalog" / "aska.yaml").is_symlink() and target.read_text() == ASKA_V1


def test_other_untracked_catalog_files_are_not_touched(svc, checker, repos):
    (repos.local / "catalog").mkdir()
    other = repos.local / "catalog" / "mine.yaml"
    other.write_text("id: mine\n")
    bundle_aska(repos)
    out = apply(svc, checker)
    assert out["updated"] and out["catalog_files_replaced"] == [] and other.read_text() == "id: mine\n"


def test_two_files_and_name_clashes_in_the_displaced_folder(svc, checker, repos):
    (repos.seed / "catalog").mkdir()
    local_aska(repos)
    (repos.local / "catalog" / "zork.yaml").write_text("id: zork\nname: Z\nimage: z:1\n")
    stale = repos.local / "data" / "catalog-displaced"
    stale.mkdir(parents=True)
    repos.remote_commit(version="0.3.1", files={"catalog/aska.yaml": ASKA_V2, "catalog/zork.yaml": "id: zork\nname: Z2\nimage: z:2\n"})
    out = apply(svc, checker)
    assert sorted(m["file"] for m in out["catalog_files_replaced"]) == ["catalog/aska.yaml", "catalog/zork.yaml"]
    assert all(Path(m["kept_at"]).exists() for m in out["catalog_files_replaced"])


@pytest.mark.parametrize("bundled,expect_reviewed", [(ASKA_V2, False), (ASKA_V1, True)])
def test_review_consequence_after_the_update(tmp_path, repos, gh, clock, restarts, bundled, expect_reviewed):
    """AI-added entry approved before: a different bundled file asks for approval again; an identical one stays approved."""
    import hashlib
    local_aska(repos, ASKA_V1)
    data = tmp_path / "data"
    data.mkdir()
    digest = hashlib.sha256(ASKA_V1.encode()).hexdigest()
    (data / "catalog-review.json").write_text(json.dumps({"aska": {
        "added_by": "mcp", "reviewed": True, "reviewed_sha256": digest, "yaml_sha256": digest}}))
    checker = up.UpdateChecker(install_dir=repos.local, http_get=gh, clock=clock, version="0.3.0", data_dir=data,
                               restart=lambda: restarts.append(1))
    svc = GameDockService(FakeBackend(), repos.local / "catalog", data, updater=checker)
    assert svc.get_catalog_entry("aska").reviewed is True
    bundle_aska(repos, bundled)
    assert checker.apply(svc.freeze, svc.unfreeze)["updated"]
    e = GameDockService(FakeBackend(), repos.local / "catalog", data).get_catalog_entry("aska")
    assert e.reviewed is expect_reviewed
    assert (data / "catalog-displaced").exists() is (not expect_reviewed) or not list((data / "catalog-displaced").iterdir())
