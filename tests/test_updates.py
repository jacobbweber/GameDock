import pytest

from gamedock.errors import BackendError, ConflictError, ValidationError
from gamedock.models import Status
from tests.conftest import ADMIN

IMAGE = "busybox:latest"


def test_image_info_before_and_after_update(service, backend, deployed):
    info = service.get_image_info(deployed)
    assert info.image == IMAGE and info.image_id.startswith("sha256:")


def test_update_needs_confirm(service, deployed):
    with pytest.raises(ValidationError, match="confirmation"):
        service.update_server_image(deployed)


def test_already_up_to_date_changes_nothing(service, backend, deployed):
    service.start(deployed)
    backend.calls.clear()
    r = service.update_server_image(deployed, confirm=True)
    assert not r.changed and r.before == r.after and "up to date" in r.note
    assert [c[0] for c in backend.calls] == ["pull"]  # not stopped, not recreated
    assert service.get_status(deployed).status == Status.RUNNING


def test_update_stopped_server(service, backend, deployed, tmp_path):
    before = service.get_image_info(deployed).image_id
    backend.publish_new_image(IMAGE)
    r = service.update_server_image(deployed, confirm=True)
    assert r.changed and r.before == before and r.after != before
    assert r.after == service.get_image_info(deployed).image_id
    assert not r.was_running and not r.restarted
    assert r.backup.kind == "pre-update"
    assert service.get_status(deployed).status == Status.STOPPED


def test_update_running_server_stops_backs_up_recreates_restarts(service, backend, deployed):
    service.start(deployed)
    backend.publish_new_image(IMAGE)
    backend.calls.clear()
    r = service.update_server_image(deployed, confirm=True)
    assert r.changed and r.was_running and r.restarted
    actions = [c[0] for c in backend.calls]
    assert actions == ["pull", "stop", "remove", "create", "start"]
    assert r.backup is not None and service.list_backups(deployed)[0].name == r.backup.name
    assert service.get_status(deployed).status == Status.RUNNING
    assert "backed up first" in r.note


def test_update_keeps_settings_and_data(service, backend, deployed, tmp_path):
    (tmp_path / "data" / "one" / "keep.txt").write_text("data")
    service.update(deployed, env={"MAX": "99"})
    backend.publish_new_image(IMAGE)
    service.update_server_image(deployed, confirm=True)
    assert (tmp_path / "data" / "one" / "keep.txt").read_text() == "data"
    assert service._spec(service._instance(deployed)).env["MAX"] == "99"


def test_failed_pull_changes_nothing_and_leaves_server_running(service, backend, deployed):
    service.start(deployed)
    backend.publish_new_image(IMAGE)
    backend.fail_next = "pull"
    backend.calls.clear()
    with pytest.raises(BackendError):
        service.update_server_image(deployed, confirm=True)
    assert [c[0] for c in backend.calls] == ["pull"]
    assert service.get_status(deployed).status == Status.RUNNING


def test_failed_backup_aborts_update_and_restarts(service, backend, deployed, monkeypatch):
    from gamedock import backups as bk
    from gamedock.errors import AccessError
    service.start(deployed)
    backend.publish_new_image(IMAGE)
    before = service.get_image_info(deployed).image_id
    monkeypatch.setattr(bk, "create_archive", lambda *a, **k: (_ for _ in ()).throw(AccessError("x")))
    with pytest.raises(AccessError):
        service.update_server_image(deployed, confirm=True)
    assert service.get_image_info(deployed).image_id == before  # not recreated
    assert service.get_status(deployed).status == Status.RUNNING


def test_failed_restart_after_update_is_a_warning(service, backend, deployed):
    service.start(deployed)
    backend.publish_new_image(IMAGE)
    real_start = backend.start

    def failing(name):
        raise BackendError("cannot start")

    backend.start = failing
    r = service.update_server_image(deployed, confirm=True)
    backend.start = real_start
    assert r.changed and not r.restarted and "could not be started" in r.warnings[0]


def test_update_method_none_is_refused(service, catalog_dir):
    (catalog_dir / "self.yaml").write_text("id: selfupd\nname: Self\nimage: x:1\nupdate_method: none\n")
    service.deploy("selfupd", "S")
    with pytest.raises(ValidationError, match="does not support"):
        service.update_server_image("s", confirm=True)


def test_catalog_update_method_validation():
    from gamedock.catalog import parse_definition
    from gamedock.errors import CatalogError
    assert parse_definition({"id": "a", "name": "n", "image": "i"}).update_method == "pull_recreate"
    with pytest.raises(CatalogError):
        parse_definition({"id": "a", "name": "n", "image": "i", "update_method": "magic"})


def test_update_blocked_while_another_operation_runs(service, deployed):
    with service._operation(deployed):
        with pytest.raises(ConflictError):
            service.update_server_image(deployed, confirm=True)
