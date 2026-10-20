import os
import tarfile
import io

import pytest

from gamedock import backups as bk
from gamedock.errors import (AccessError, ConflictError, NotFoundError, OperationError, ValidationError)
from gamedock.models import Status
from tests.conftest import ADMIN

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="permission errors cannot be provoked as root")


@pytest.fixture
def srv(service, deployed, tmp_path):
    """Server 'one' (stopped) with a couple of data files."""
    d = tmp_path / "data" / "one"
    (d / "saves").mkdir(parents=True, exist_ok=True)
    (d / "saves" / "world.dat").write_text("world v1")
    (d / "notes.txt").write_text("hello")
    return service


def data(tmp_path):
    return tmp_path / "data" / "one"


# ------------------------------------------------------------------ create / list
def test_create_backup_of_stopped_server(srv, tmp_path):
    r = srv.create_backup("one")
    assert r.backup.name.startswith("one-20260101-") and r.backup.name.endswith(".tar.gz")
    assert r.backup.kind == "manual" and r.backup.size > 0
    assert not r.was_running and not r.stopped_for_backup
    assert "consistent" in r.note
    assert (tmp_path / "data" / "backups" / "one" / r.backup.name).is_file()
    with tarfile.open(r.backup.path) as t:
        assert {"saves/world.dat", "notes.txt"} <= set(t.getnames())
    assert [b.name for b in srv.list_backups("one")] == [r.backup.name]


def test_backup_is_outside_the_data_folder(srv, tmp_path):
    r = srv.create_backup("one")
    assert data(tmp_path).resolve() not in __import__("pathlib").Path(r.backup.path).resolve().parents


def test_running_server_is_stopped_backed_up_and_restarted_by_default(srv, backend):
    srv.start("one")
    backend.calls.clear()
    r = srv.create_backup("one")
    assert r.was_running and r.stopped_for_backup and r.restarted and not r.live
    assert "stopped for a consistent backup" in r.note and "started again" in r.note
    assert [c[0] for c in backend.calls if c[0] in ("stop", "start")] == ["stop", "start"]
    assert srv.get_status("one").status == Status.RUNNING


def test_live_backup_does_not_stop_and_says_so(srv, backend):
    srv.start("one")
    backend.calls.clear()
    r = srv.create_backup("one", live=True)
    assert r.live and not r.stopped_for_backup
    assert "NOT stopped" in r.note and "inconsistent" in r.note
    assert not any(c[0] == "stop" for c in backend.calls)


def test_failed_backup_restarts_server_and_leaves_no_file(srv, tmp_path, monkeypatch):
    srv.start("one")
    monkeypatch.setattr(bk, "create_archive", lambda *a, **k: (_ for _ in ()).throw(OperationError("disk full")))
    with pytest.raises(OperationError):
        srv.create_backup("one")
    assert srv.get_status("one").status == Status.RUNNING


def test_restart_failure_after_backup_is_a_warning(srv, backend):
    srv.start("one")
    orig = backend.start

    def failing(name):
        raise __import__("gamedock.errors", fromlist=["BackendError"]).BackendError("nope")

    backend.start = failing
    r = srv.create_backup("one")
    assert r.backup and r.warnings and "could not be started" in r.warnings[0]
    backend.start = orig


def test_empty_or_missing_data_folder(service, deployed, tmp_path):
    import shutil
    shutil.rmtree(tmp_path / "data" / "one")
    with pytest.raises(ValidationError, match="no data folder"):
        service.create_backup("one")


@needs_non_root
def test_unreadable_file_gives_clear_error_and_saves_nothing(srv, tmp_path):
    secret = data(tmp_path) / "saves" / "root_owned.dat"
    secret.write_text("x")
    secret.chmod(0)
    srv.backend.ownership_fail = "simulated helper failure"   # 0.6.2: only when the automatic repair fails too
    try:
        with pytest.raises(AccessError, match="(?s)another user.*PUID.*tried to give the files back.*simulated helper"):
            srv.create_backup("one")
    finally:
        secret.chmod(0o644)
    bdir = tmp_path / "data" / "backups" / "one"
    assert list(bdir.iterdir()) == []  # no partial file left behind


@needs_non_root
def test_unreadable_directory_gives_clear_error(srv, tmp_path):
    d = data(tmp_path) / "locked"
    d.mkdir()
    (d / "f").write_text("x")
    d.chmod(0)
    srv.backend.ownership_fail = "simulated helper failure"
    try:
        with pytest.raises(AccessError):
            srv.create_backup("one")
    finally:
        d.chmod(0o755)


def test_same_second_backups_get_unique_names(srv, tmp_path):
    from datetime import datetime, timezone
    now = datetime(2026, 5, 5, 5, 5, 5, tzinfo=timezone.utc)
    a = bk.create_archive(data(tmp_path), tmp_path / "b", "one", now)
    b = bk.create_archive(data(tmp_path), tmp_path / "b", "one", now)
    c = bk.create_archive(data(tmp_path), tmp_path / "b", "one", now)
    assert len({a.name, b.name, c.name}) == 3
    assert [x.name for x in bk.list_backups(tmp_path / "b")] == [c.name, b.name, a.name]  # newest first


# ------------------------------------------------------------------ retention
def test_retention_keeps_newest_n(srv):
    srv.backup_retention = 3
    names = [srv.create_backup("one").backup.name for _ in range(5)]
    remaining = [b.name for b in srv.list_backups("one")]
    assert remaining == names[:1:-1]  # the 3 newest, newest first
    last = srv.create_backup("one")
    assert len(last.removed_by_retention) == 1 and "Removed 1 old backup" in last.note


def test_default_retention_is_ten(service):
    assert service.backup_retention == 10


def test_retention_setting_from_env(monkeypatch):
    from gamedock.config import Settings
    monkeypatch.setenv("GAMEDOCK_BACKUP_RETENTION", "4")
    assert Settings.from_env().backup_retention == 4
    monkeypatch.setenv("GAMEDOCK_BACKUP_RETENTION", "0")
    assert Settings.from_env().backup_retention == 1


# ------------------------------------------------------------------ restore
def test_restore_replaces_data_and_takes_safety_backup(srv, tmp_path):
    b = srv.create_backup("one").backup
    (data(tmp_path) / "saves" / "world.dat").write_text("world v2 (newer)")
    (data(tmp_path) / "extra.txt").write_text("added later")
    r = srv.restore_backup("one", b.name, confirm=True)
    assert (data(tmp_path) / "saves" / "world.dat").read_text() == "world v1"
    assert not (data(tmp_path) / "extra.txt").exists()
    assert r.safety_backup.kind == "pre-restore" and "pre-restore" in r.safety_backup.name
    # the safety backup holds what was there just before the restore
    with tarfile.open(r.safety_backup.path) as t:
        assert t.extractfile("saves/world.dat").read() == b"world v2 (newer)"
        assert "extra.txt" in t.getnames()
    assert b.name in [x.name for x in srv.list_backups("one")]


def test_restore_needs_confirm_and_stopped_server(srv):
    b = srv.create_backup("one").backup
    with pytest.raises(ValidationError, match="confirmation"):
        srv.restore_backup("one", b.name)
    srv.start("one")
    with pytest.raises(ConflictError, match="Stop the server"):
        srv.restore_backup("one", b.name, confirm=True)


def test_restore_unknown_or_malicious_names(srv):
    with pytest.raises(NotFoundError):
        srv.restore_backup("one", "one-20200101-000000.tar.gz", confirm=True)
    for bad in ["../../etc/passwd", "../instances.json", "/etc/passwd", "x.tar.gz", "one-20200101-000000.tar.gz/../x"]:
        with pytest.raises((ValidationError, NotFoundError)):
            srv.restore_backup("one", bad, confirm=True)


def test_failed_safety_backup_aborts_restore(srv, tmp_path, monkeypatch):
    b = srv.create_backup("one").backup
    (data(tmp_path) / "notes.txt").write_text("keep me")
    real = bk.create_archive

    def boom(*a, **k):
        raise AccessError("simulated unreadable files")

    monkeypatch.setattr(bk, "create_archive", boom)
    with pytest.raises(AccessError):
        srv.restore_backup("one", b.name, confirm=True)
    monkeypatch.setattr(bk, "create_archive", real)
    assert (data(tmp_path) / "notes.txt").read_text() == "keep me"  # untouched


def test_restore_not_pruned_by_retention_of_its_own_safety_backup(srv, tmp_path):
    srv.backup_retention = 1
    b = srv.create_backup("one").backup
    srv.restore_backup("one", b.name, confirm=True)
    names = [x.name for x in srv.list_backups("one")]
    assert b.name in names  # protected while being restored


def _evil_archive(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, kind, extra in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = 1
                t.addfile(ti, io.BytesIO(b"x"))
            elif kind == "sym":
                ti.type = tarfile.SYMTYPE
                ti.linkname = extra
                t.addfile(ti)
            elif kind == "hard":
                ti.type = tarfile.LNKTYPE
                ti.linkname = extra
                t.addfile(ti)


@pytest.mark.parametrize("members", [
    [("../escape.txt", "file", None)],
    [("/abs.txt", "file", None)],
    [("a/../../escape.txt", "file", None)],
    [("link", "sym", "/etc")],
    [("link", "sym", "../../outside")],
    [("h", "hard", "/etc/passwd")],
])
def test_restore_refuses_unsafe_archives(srv, tmp_path, members):
    bdir = tmp_path / "data" / "backups" / "one"
    bdir.mkdir(parents=True, exist_ok=True)
    _evil_archive(bdir / "one-20200101-000000.tar.gz", members)
    with pytest.raises(ValidationError, match="refusing"):
        srv.restore_backup("one", "one-20200101-000000.tar.gz", confirm=True)
    assert (data(tmp_path) / "notes.txt").read_text() == "hello"
    assert not (tmp_path / "data" / "escape.txt").exists() and not (tmp_path / "escape.txt").exists()


def test_corrupt_archive_leaves_data_alone(srv, tmp_path):
    bdir = tmp_path / "data" / "backups" / "one"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "one-20200101-000000.tar.gz").write_bytes(b"not a tar file")
    with pytest.raises(OperationError, match="damaged"):
        srv.restore_backup("one", "one-20200101-000000.tar.gz", confirm=True)
    assert (data(tmp_path) / "notes.txt").read_text() == "hello"


# ------------------------------------------------------------------ delete backup
def test_delete_backup_requires_confirm(srv):
    b = srv.create_backup("one").backup
    with pytest.raises(ValidationError):
        srv.delete_backup("one", b.name)
    assert len(srv.list_backups("one")) == 1
    srv.delete_backup("one", b.name, confirm=True)
    assert srv.list_backups("one") == []
    with pytest.raises(NotFoundError):
        srv.delete_backup("one", b.name, confirm=True)


def test_delete_backup_rejects_path_tricks(srv, tmp_path):
    for bad in ["../../instances.json", "../one-20200101-000000.tar.gz", "/etc/passwd"]:
        with pytest.raises((ValidationError, NotFoundError)):
            srv.delete_backup("one", bad, confirm=True)
    assert (tmp_path / "data" / "instances.json").exists()


# ------------------------------------------------------------------ wipe data
def test_wipe_data_backs_up_first(srv, tmp_path):
    r = srv.wipe_data("one", confirm=True)
    assert r.safety_backup.kind == "pre-wipe"
    assert not (data(tmp_path) / "notes.txt").exists()
    assert (data(tmp_path) / "saves").is_dir()  # volume folders are recreated
    with tarfile.open(r.safety_backup.path) as t:
        assert "notes.txt" in t.getnames()


def test_wipe_needs_confirm_and_stopped(srv):
    with pytest.raises(ValidationError):
        srv.wipe_data("one")
    srv.start("one")
    with pytest.raises(ConflictError):
        srv.wipe_data("one", confirm=True)


def test_failed_backup_aborts_wipe(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "create_archive", lambda *a, **k: (_ for _ in ()).throw(AccessError("no")))
    with pytest.raises(AccessError):
        srv.wipe_data("one", confirm=True)
    assert (data(tmp_path) / "notes.txt").read_text() == "hello"


# ------------------------------------------------------------------ delete with data
def test_delete_with_data_backs_up_outside_data_folder_then_deletes(srv, tmp_path, backend):
    r = srv.delete("one", confirm=True, delete_data=True, confirm_delete_data=True)
    assert r.data_deleted and r.backup.kind == "pre-delete"
    assert "/backups/_deleted/" in r.backup.path
    assert not data(tmp_path).exists()
    assert srv.list_servers() == []
    assert ("remove", "gamedock-one") in backend.calls
    with tarfile.open(r.backup.path) as t:
        assert t.extractfile("saves/world.dat").read() == b"world v1"


def test_failed_backup_aborts_delete_entirely(srv, tmp_path, backend, monkeypatch):
    monkeypatch.setattr(bk, "create_archive", lambda *a, **k: (_ for _ in ()).throw(AccessError("unreadable")))
    backend.calls.clear()
    with pytest.raises(AccessError):
        srv.delete("one", confirm=True, delete_data=True, confirm_delete_data=True)
    assert len(srv.list_servers()) == 1
    assert (data(tmp_path) / "notes.txt").exists()
    assert not any(c[0] == "remove" for c in backend.calls)  # container untouched too


def test_delete_without_data_keeps_folder_and_takes_no_backup(srv, tmp_path):
    r = srv.delete("one", confirm=True)
    assert not r.data_deleted and r.backup is None and "kept" in r.note
    assert (data(tmp_path) / "notes.txt").exists()
    assert not (tmp_path / "data" / "backups" / "_deleted").exists()


def test_delete_with_data_requires_both_confirmations(srv, tmp_path):
    with pytest.raises(ValidationError):
        srv.delete("one", delete_data=True, confirm_delete_data=True)  # no confirm
    with pytest.raises(ValidationError, match="second"):
        srv.delete("one", confirm=True, delete_data=True)
    assert (data(tmp_path) / "notes.txt").exists()


def test_delete_with_data_refused_while_running(srv):
    srv.start("one")
    with pytest.raises(ConflictError):
        srv.delete("one", confirm=True, delete_data=True, confirm_delete_data=True)


# ------------------------------------------------------------------ misc
def test_backups_id_is_reserved(service):
    info = service.deploy("sample", "Backups", env=ADMIN)
    assert info.id == "backups-2"


def test_operations_on_one_server_do_not_overlap(srv):
    with srv._operation("one"):
        with pytest.raises(ConflictError, match="Another operation"):
            srv.start("one")
        with pytest.raises(ConflictError):
            srv.create_backup("one")
    srv.start("one")  # free again


def test_backup_ops_unknown_server(service):
    with pytest.raises(NotFoundError):
        service.list_backups("nope")
    with pytest.raises(NotFoundError):
        service.create_backup("nope")


# ------------------------------------------------------------------ review pass
def test_archives_are_private(tmp_path):
    import os
    from datetime import datetime, timezone
    src = tmp_path / "d"
    src.mkdir()
    (src / "a.txt").write_text("secret admin password")
    info = bk.create_archive(src, tmp_path / "b", "x", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert os.stat(info.path).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / "b").st_mode & 0o777 == 0o700


def test_ownership_hint_quotes_odd_paths(tmp_path):
    p = tmp_path / "it's a dir"
    hint = bk.ownership_hint(p)
    assert "chown -R $(id -u):$(id -g)" in hint and "PUID" in hint
    import shlex
    assert shlex.split(hint.split("or with sudo: ")[1].split("). Then")[0])[-1] == str(p)  # copy-paste safe
    docker_cmd = hint.split("can): ")[1].split(" (or with sudo")[0]
    assert shlex.split(docker_cmd)[:4] == ["docker", "run", "--rm", "--network"]
    assert f"type=bind,source={p},target=/d" in shlex.split(docker_cmd)  # the odd path survives quoting


def test_disk_full_while_backing_up_is_explained(tmp_path, monkeypatch):
    import errno
    from datetime import datetime, timezone
    src = tmp_path / "d"
    src.mkdir()
    (src / "a.txt").write_text("x")

    def boom(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(bk.tarfile, "open", boom)
    with pytest.raises(bk.OperationError, match="Not enough free disk space"):
        bk.create_archive(src, tmp_path / "b", "x", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert not list((tmp_path / "b").glob(".*partial"))


def test_restore_warns_when_old_files_belonged_to_another_user(service, deployed, tmp_path, monkeypatch):
    from gamedock import service as svc_mod
    service.create_backup("one")
    name = service.list_backups("one")[0].name
    monkeypatch.setattr(svc_mod, "foreign_owners", lambda root: ({1234}, str(root / "saves" / "w.dat")))
    service.backend.ownership_fail = "no helper"   # 0.6.2: the warning is only left when the automatic repair fails
    r = service.restore_backup("one", name, confirm=True)
    assert any("user id 1234" in w and "chown -R 1234" in w for w in r.warnings)
