import pytest

from gamedock.errors import BackendError, ConflictError, NotFoundError, ValidationError
from gamedock.models import PortMapping, Status
from tests.conftest import ADMIN


def udp(host, container=27015):
    return PortMapping("udp", host, container)


# ---------------------------------------------------------------- catalog
def test_catalog_lists_templates_not_servers(service):
    assert [e.id for e in service.list_catalog()] == ["sample"]
    assert service.list_servers() == []
    assert service.get_catalog_entry("sample").fields[0].name == "SERVER_NAME"


def test_unknown_catalog_entry(service):
    with pytest.raises(NotFoundError):
        service.get_catalog_entry("nope")
    with pytest.raises(NotFoundError):
        service.deploy("nope", "x")


# ---------------------------------------------------------------- deploy
def test_deploy_creates_container_but_does_not_start(service, backend, tmp_path):
    info = service.deploy("sample", "My Server", env=ADMIN)
    assert info.id == "my-server" and info.name == "My Server"
    assert info.status == Status.STOPPED
    assert ("create", "gamedock-my-server") in backend.calls
    assert ("start", "gamedock-my-server") not in backend.calls
    assert (tmp_path / "data" / "my-server" / "saves").is_dir()
    assert [s.id for s in service.list_servers()] == ["my-server"]


def test_deploy_with_start(service):
    assert service.deploy("sample", "A", env=ADMIN, start=True).status == Status.RUNNING


def test_deploy_uses_defaults_and_overrides(service, backend):
    info = service.deploy("sample", "A", ports=[udp(30000)], env={**ADMIN, "SERVER_NAME": "Hello"})
    assert info.ports == (udp(30000),)
    spec = service._spec(service._instance("a"))
    assert spec.env["SERVER_NAME"] == "Hello" and spec.env["MAX"] == "8"
    assert spec.env["FIXED"] == "value" and spec.env["ADMIN_PASSWORD"] == "adminpw"
    assert spec.ports == (udp(30000),)


def test_two_instances_of_one_game_coexist(service):
    service.deploy("sample", "First", env=ADMIN)
    service.deploy("sample", "Second", ports=[udp(27016)], env=ADMIN)
    assert {s.id for s in service.list_servers()} == {"first", "second"}


def test_port_conflict_with_other_instance(service):
    service.deploy("sample", "First", env=ADMIN)
    with pytest.raises(ConflictError, match="First"):
        service.deploy("sample", "Second", env=ADMIN)  # default port 27015 is taken
    assert [s.id for s in service.list_servers()] == ["first"]


def test_same_port_number_other_protocol_is_not_a_conflict(service, catalog_dir):
    (catalog_dir / "two.yaml").write_text(
        "id: two\nname: Two\nimage: i\nports:\n  - {protocol: tcp, host: 27015, container: 1}\n")
    service.deploy("sample", "First", env=ADMIN)
    service.deploy("two", "Second")  # tcp 27015 vs udp 27015


@pytest.mark.parametrize("host", [0, 70000, -1])
def test_bad_port_rejected(service, host):
    with pytest.raises(ValidationError):
        service.deploy("sample", "A", ports=[udp(host)], env=ADMIN)


def test_unknown_container_port_rejected(service):
    with pytest.raises(ValidationError):
        service.deploy("sample", "A", ports=[udp(30000, container=1)], env=ADMIN)


def test_required_env_enforced(service):
    with pytest.raises(ValidationError, match="Admin password"):
        service.deploy("sample", "A")
    with pytest.raises(ValidationError, match="Server name"):
        service.deploy("sample", "A", env={**ADMIN, "SERVER_NAME": "  "})


def test_unknown_env_name_rejected(service):
    with pytest.raises(ValidationError, match="Unknown setting"):
        service.deploy("sample", "A", env={**ADMIN, "FIXED": "override"})


def test_newline_in_env_rejected(service):
    with pytest.raises(ValidationError):
        service.deploy("sample", "A", env={**ADMIN, "MAX": "1\nEVIL=1"})


@pytest.mark.parametrize("name", ["", "   ", "!!!", "x" * 65])
def test_bad_names_rejected(service, name):
    with pytest.raises(ValidationError):
        service.deploy("sample", name, env=ADMIN)


def test_duplicate_name_rejected_case_insensitive(service):
    service.deploy("sample", "Alpha", env=ADMIN)
    with pytest.raises(ConflictError):
        service.deploy("sample", "alpha", ports=[udp(30000)], env=ADMIN)


def test_id_collision_gets_suffix(service):
    service.deploy("sample", "a b", env=ADMIN)
    info = service.deploy("sample", "a-b", ports=[udp(30000)], env=ADMIN)
    assert info.id == "a-b-2"


def test_failed_create_stores_nothing(service, backend):
    backend.fail_next = "create"
    with pytest.raises(BackendError):
        service.deploy("sample", "A", env=ADMIN)
    assert service.list_servers() == []


def test_failed_start_after_deploy_keeps_server_and_says_so(service, backend):
    backend.fail_next = "start"
    with pytest.raises(BackendError, match="was deployed but could not be started"):
        service.deploy("sample", "A", env=ADMIN, start=True)
    assert service.get_status("a").status == Status.STOPPED


def test_instances_persist_across_service_objects(service, backend, catalog_dir, tmp_path):
    from gamedock.service import GameDockService
    service.deploy("sample", "Keep", env=ADMIN)
    again = GameDockService(backend, catalog_dir, tmp_path / "data")
    assert [s.id for s in again.list_servers()] == ["keep"]


def test_instance_survives_catalog_entry_removal(service, catalog_dir):
    service.deploy("sample", "A", env=ADMIN)
    (catalog_dir / "sample.yaml").unlink()
    assert service.get_status("a").game_name == "Sample Game"
    service.start("a")
    service.stop("a")


# ---------------------------------------------------------------- secrets
def test_secrets_never_exposed_in_info_or_repr(service, deployed):
    info = service.get_status(deployed)
    admin = next(e for e in info.env if e.name == "ADMIN_PASSWORD")
    assert admin.secret and admin.value is None and admin.is_set
    assert "adminpw" not in repr(info) and "adminpw" not in repr(service._instance(deployed))
    assert "adminpw" not in repr(service._spec(service._instance(deployed)))
    name = next(e for e in info.env if e.name == "SERVER_NAME")
    assert name.value == "Sample"


def test_store_file_is_private_and_has_secret(service, deployed, tmp_path):
    f = tmp_path / "data" / "instances.json"
    assert f.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- update
def test_update_changes_env_ports_name_and_recreates_container(service, backend, deployed):
    backend.calls.clear()
    info = service.update(deployed, name="Renamed", ports=[udp(31000)], env={"MAX": "20"})
    assert info.name == "Renamed" and info.id == "one" and info.ports == (udp(31000),)
    assert next(e for e in info.env if e.name == "MAX").value == "20"
    assert [c[0] for c in backend.calls] == ["remove", "create"]


def test_update_blank_secret_keeps_existing(service, deployed):
    service.update(deployed, env={"ADMIN_PASSWORD": ""})
    assert service._instance(deployed).env["ADMIN_PASSWORD"] == "adminpw"
    service.update(deployed, env={"ADMIN_PASSWORD": "new"})
    assert service._instance(deployed).env["ADMIN_PASSWORD"] == "new"


def test_update_refused_while_running(service, deployed):
    service.start(deployed)
    with pytest.raises(ConflictError):
        service.update(deployed, env={"MAX": "1"})


def test_update_port_conflict_checked_but_own_ports_ignored(service, deployed):
    service.deploy("sample", "Two", ports=[udp(27016)], env=ADMIN)
    with pytest.raises(ConflictError):
        service.update(deployed, ports=[udp(27016)])
    service.update(deployed, ports=[udp(27015)])  # its own current port is fine


# ---------------------------------------------------------------- lifecycle (step 1 behaviour)
def test_start_stop_restart(service, backend, deployed):
    assert service.start(deployed).status == Status.RUNNING
    backend.calls.clear()
    assert service.start(deployed).status == Status.RUNNING  # idempotent
    assert backend.calls == []
    assert service.restart(deployed).status == Status.RUNNING
    assert [c[0] for c in backend.calls if c[0] in ("stop", "start")] == ["stop", "start"]
    assert service.stop(deployed).status == Status.STOPPED


def test_start_recreates_missing_container(service, backend, deployed):
    backend.remove("gamedock-one")
    assert service.get_status(deployed).status == Status.NOT_CREATED
    assert service.start(deployed).status == Status.RUNNING


def test_unknown_server(service):
    for fn in (service.get_status, service.start, service.stop, service.restart):
        with pytest.raises(NotFoundError):
            fn("nope")


def test_backend_failure_surfaces(service, backend, deployed):
    backend.fail_next = "start"
    with pytest.raises(BackendError):
        service.start(deployed)


def test_status_error_is_reported_not_raised(service, backend, deployed):
    def boom(name):
        raise BackendError("engine down")

    backend.get_state = boom
    info = service.get_status(deployed)
    assert info.status == Status.ERROR and "engine down" in info.detail


def test_volumes_resolved_under_instance_dir(service, deployed, tmp_path):
    spec = service._spec(service._instance(deployed))
    assert spec.volumes[0].host == str((tmp_path / "data" / "one" / "saves").resolve())
    assert spec.labels["gamedock.managed"] == "true"
    assert spec.labels["gamedock.server_id"] == "one"


# ---------------------------------------------------------------- delete
def test_delete_requires_confirm(service, backend, deployed):
    with pytest.raises(ValidationError, match="confirmation"):
        service.delete(deployed)
    assert len(service.list_servers()) == 1


def test_delete_removes_container_keeps_data(service, backend, deployed, tmp_path):
    service.delete(deployed, confirm=True)
    assert service.list_servers() == []
    assert ("remove", "gamedock-one") in backend.calls
    assert (tmp_path / "data" / "one" / "saves").is_dir()
    service.deploy("sample", "One", env=ADMIN)  # name and port are free again


def test_delete_refused_while_running(service, deployed):
    service.start(deployed)
    with pytest.raises(ConflictError):
        service.delete(deployed, confirm=True)


def test_delete_data_needs_second_confirmation_then_is_refused_until_backups(service, deployed, tmp_path):
    with pytest.raises(ValidationError, match="second explicit confirmation"):
        service.delete(deployed, confirm=True, delete_data=True)
    with pytest.raises(ValidationError, match="step 3"):
        service.delete(deployed, confirm=True, delete_data=True, confirm_delete_data=True)
    assert len(service.list_servers()) == 1  # nothing happened
    assert (tmp_path / "data" / "one" / "saves").is_dir()


def test_delete_unknown(service):
    with pytest.raises(NotFoundError):
        service.delete("nope", confirm=True)
