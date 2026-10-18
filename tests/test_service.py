import pytest

from gamedock.errors import BackendError, NotFoundError
from gamedock.models import Status


def test_list_shows_catalog_entries_as_not_created(service):
    servers = service.list_servers()
    assert [s.id for s in servers] == ["sample"]
    assert servers[0].status == Status.NOT_CREATED
    assert servers[0].ports[0].host == 27015


def test_start_creates_and_runs(service, backend, tmp_path):
    info = service.start("sample")
    assert info.status == Status.RUNNING
    assert ("create", "gamedock-sample") in backend.calls
    assert (tmp_path / "data" / "sample" / "saves").is_dir()  # host volume folder made


def test_start_is_idempotent(service, backend):
    service.start("sample")
    backend.calls.clear()
    assert service.start("sample").status == Status.RUNNING
    assert backend.calls == []


def test_stop(service):
    service.start("sample")
    assert service.stop("sample").status == Status.STOPPED


def test_stop_when_not_created_is_noop(service, backend):
    assert service.stop("sample").status == Status.NOT_CREATED
    assert ("stop", "gamedock-sample") not in backend.calls


def test_restart(service, backend):
    service.start("sample")
    backend.calls.clear()
    assert service.restart("sample").status == Status.RUNNING
    assert [c[0] for c in backend.calls if c[0] in ("stop", "start")] == ["stop", "start"]


def test_unknown_server(service):
    with pytest.raises(NotFoundError):
        service.get_status("nope")
    with pytest.raises(NotFoundError):
        service.start("nope")


def test_backend_failure_surfaces(service, backend):
    backend.fail_next = "start"
    with pytest.raises(BackendError):
        service.start("sample")


def test_status_error_is_reported_not_raised(service, backend):
    def boom(name):
        raise BackendError("engine down")

    backend.get_state = boom
    info = service.get_status("sample")
    assert info.status == Status.ERROR and "engine down" in info.detail


def test_spec_resolves_relative_volumes_under_data_dir(service, tmp_path):
    spec = service._spec(service._definition("sample"))
    assert spec.volumes[0].host == str((tmp_path / "data" / "sample" / "saves").resolve())
    assert spec.labels["gamedock.managed"] == "true"
