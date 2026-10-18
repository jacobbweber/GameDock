import os

import pytest

from gamedock import configfiles as cf
from gamedock.catalog import parse_definition
from gamedock.errors import (AccessError, ConflictError, NotFoundError, TooLargeError, ValidationError)

INI = "conf/settings.ini"
HOT = "conf/hot.txt"
JSON = "conf/data.json"

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="permission errors cannot be provoked as root")


@pytest.fixture
def srv(service, deployed, tmp_path):
    d = tmp_path / "data" / "one" / "conf"
    d.mkdir(parents=True)
    (d / "settings.ini").write_text("Name=Hello\nAdminPassword=hunter2\nrcon.password=\"pw 2\"\n")
    (d / "hot.txt").write_text("a\n")
    (d / "data.json").write_text('{"token": "abc123", "n": 1}')
    return service


def root(tmp_path):
    return tmp_path / "data" / "one"


# ------------------------------------------------------------------ listing / reading
def test_list_declared_files(srv):
    files = {f.path: f for f in srv.list_config_files("one")}
    assert set(files) == {INI, HOT, JSON}
    assert files[INI].exists and files[INI].size > 0 and files[HOT].hot_edit and not files[INI].hot_edit


def test_read_masks_sensitive_values(srv):
    r = srv.read_config_file("one", INI)
    assert "hunter2" not in r.content and "pw 2" not in r.content
    assert "Name=Hello" in r.content
    assert r.content.count(cf.MASK) == 2 and r.masked
    assert 'rcon.password="********"' in r.content
    j = srv.read_config_file("one", JSON)
    assert "abc123" not in j.content and '"token": "********"' in j.content


def test_read_missing_file_is_empty_and_creatable(service, deployed):
    r = service.read_config_file("one", INI)
    assert not r.info.exists and r.content == "" and "has not created its settings file yet" in r.note


def test_only_declared_files_are_accessible(srv, tmp_path):
    (root(tmp_path) / "other.txt").write_text("x")
    for bad in ["other.txt", "../instances.json", "/etc/passwd", "conf/../other.txt", ""]:
        with pytest.raises(NotFoundError):
            srv.read_config_file("one", bad)
        with pytest.raises(NotFoundError):
            srv.write_config_file("one", bad, "x")


# ------------------------------------------------------------------ writing
def test_write_makes_timestamped_bak_and_atomic_replace(srv, tmp_path):
    r = srv.write_config_file("one", HOT, "b\n")
    assert (root(tmp_path) / HOT).read_text() == "b\n"
    assert r.backup and r.backup.startswith("hot.txt.20260101-") and r.backup.endswith(".bak")
    assert (root(tmp_path) / "conf" / r.backup).read_text() == "a\n"
    r2 = srv.write_config_file("one", HOT, "c\n")
    assert r2.backup != r.backup
    assert (root(tmp_path) / "conf" / r2.backup).read_text() == "b\n"
    assert srv.list_config_files("one")[1].backups[0] == r2.backup  # newest first
    assert not [p for p in (root(tmp_path) / "conf").iterdir() if p.name.endswith(".tmp")]


def test_bak_count_is_limited(srv, tmp_path):
    for i in range(cf.KEEP_BAKS + 5):
        srv.write_config_file("one", HOT, f"v{i}\n")
    baks = [p for p in (root(tmp_path) / "conf").iterdir() if p.name.endswith(".bak")]
    assert len(baks) == cf.KEEP_BAKS


def test_write_creates_missing_file_without_bak(service, deployed, tmp_path):
    r = service.write_config_file("one", INI, "Name=New\n")
    assert r.backup is None
    assert (root(tmp_path) / INI).read_text() == "Name=New\n"


def test_write_refused_while_running_unless_hot_edit(srv, tmp_path):
    srv.start("one")
    with pytest.raises(ConflictError, match="Stop the server"):
        srv.write_config_file("one", INI, "x")
    assert "hunter2" in (root(tmp_path) / INI).read_text()
    r = srv.write_config_file("one", HOT, "live edit\n")
    assert r.running and "Restart the server" in r.note
    c = srv.read_config_file("one", INI)
    assert c.running and not c.editable_now
    assert srv.read_config_file("one", HOT).editable_now


def test_masked_placeholders_keep_real_values_on_save(srv, tmp_path):
    shown = srv.read_config_file("one", INI).content
    srv.write_config_file("one", INI, shown.replace("Hello", "Changed"))
    text = (root(tmp_path) / INI).read_text()
    assert "Name=Changed" in text and "AdminPassword=hunter2" in text and 'rcon.password="pw 2"' in text
    assert cf.MASK not in text


def test_user_can_replace_a_masked_value(srv, tmp_path):
    shown = srv.read_config_file("one", INI).content
    srv.write_config_file("one", INI, shown.replace("AdminPassword=********", "AdminPassword=newpass"))
    text = (root(tmp_path) / INI).read_text()
    assert "AdminPassword=newpass" in text and 'rcon.password="pw 2"' in text


def test_placeholder_with_no_stored_value_is_rejected(service, deployed):
    with pytest.raises(ValidationError, match="placeholder"):
        service.write_config_file("one", INI, "AdminPassword=********\n")


def test_json_files_are_validated_and_masked_roundtrip(srv, tmp_path):
    with pytest.raises(ValidationError, match="Not valid JSON"):
        srv.write_config_file("one", JSON, "{oops")
    shown = srv.read_config_file("one", JSON).content
    srv.write_config_file("one", JSON, shown.replace('"n": 1', '"n": 2'))
    import json
    assert json.loads((root(tmp_path) / JSON).read_text()) == {"token": "abc123", "n": 2}


def test_inline_palworld_style_values_are_masked():
    text = 'OptionSettings=(ServerName="X",AdminPassword="secret1",ServerPassword="",Port=8211)'
    masked = cf.mask_text(text, ("AdminPassword", "ServerPassword"))
    assert "secret1" not in masked and 'AdminPassword="********"' in masked
    assert 'ServerPassword=""' in masked  # empty values are left alone
    assert cf.unmask_text(masked, text, ("AdminPassword", "ServerPassword")) == text


def test_key_substring_is_not_masked():
    assert cf.mask_text("myAdminPassword=1\nrcon.password=2\n", ("password",)) == "myAdminPassword=1\nrcon.password=2\n"


def test_size_limits(srv, tmp_path):
    with pytest.raises(TooLargeError):
        srv.write_config_file("one", HOT, "x" * (cf.MAX_CONFIG_BYTES + 1))
    big = root(tmp_path) / HOT
    big.write_text("y" * (cf.MAX_CONFIG_BYTES + 1))
    with pytest.raises(TooLargeError):
        srv.read_config_file("one", HOT)


def test_non_utf8_and_nul(srv, tmp_path):
    (root(tmp_path) / HOT).write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ValidationError, match="UTF-8"):
        srv.read_config_file("one", HOT)
    with pytest.raises(ValidationError):
        srv.write_config_file("one", HOT, "a\0b")


def test_line_endings_preserved(srv, tmp_path):
    srv.write_config_file("one", HOT, "a\r\nb\r\n")
    assert (root(tmp_path) / HOT).read_bytes() == b"a\r\nb\r\n"


# ------------------------------------------------------------------ traversal / symlinks
def test_symlinked_directory_escape_is_refused(srv, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "settings.ini").write_text("SECRET=stolen")
    import shutil
    shutil.rmtree(root(tmp_path) / "conf")
    os.symlink(outside, root(tmp_path) / "conf")
    with pytest.raises(ValidationError, match="escapes"):
        srv.read_config_file("one", INI)
    with pytest.raises(ValidationError, match="escapes"):
        srv.write_config_file("one", INI, "pwned")
    assert (outside / "settings.ini").read_text() == "SECRET=stolen"


def test_symlinked_file_escape_is_refused(srv, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("original")
    (root(tmp_path) / HOT).unlink()
    os.symlink(target, root(tmp_path) / HOT)
    with pytest.raises(ValidationError):
        srv.read_config_file("one", HOT)
    with pytest.raises(ValidationError):
        srv.write_config_file("one", HOT, "pwned")
    assert target.read_text() == "original"


def test_symlink_inside_data_folder_is_still_refused(srv, tmp_path):
    (root(tmp_path) / "real.txt").write_text("r")
    (root(tmp_path) / HOT).unlink()
    os.symlink(root(tmp_path) / "real.txt", root(tmp_path) / HOT)
    with pytest.raises(ValidationError, match="symlink"):
        srv.write_config_file("one", HOT, "x")


def test_safe_path_unit(tmp_path):
    for bad in ["../x", "a/../../x", "/etc/passwd", "", "a\0b"]:
        with pytest.raises(ValidationError):
            cf.safe_path(tmp_path, bad)
    assert cf.safe_path(tmp_path, "a/b.txt") == tmp_path.resolve() / "a" / "b.txt"


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a/../../b", "a\\b"])
def test_catalog_rejects_bad_config_paths(path):
    from gamedock.errors import CatalogError
    with pytest.raises(CatalogError):
        parse_definition({"id": "a", "name": "n", "image": "i", "config_files": [{"path": path}]})


def test_catalog_config_validation():
    from gamedock.errors import CatalogError
    with pytest.raises(CatalogError):
        parse_definition({"id": "a", "name": "n", "image": "i", "config_files": [{"path": "x", "format": "xml"}]})
    with pytest.raises(CatalogError):
        parse_definition({"id": "a", "name": "n", "image": "i", "config_files": [{"path": "x"}, {"path": "x"}]})
    with pytest.raises(CatalogError):
        parse_definition({"id": "a", "name": "n", "image": "i",
                          "config_files": [{"path": "x", "sensitive_keys": ["bad key!"]}]})


@needs_non_root
def test_write_permission_error_is_clear(srv, tmp_path):
    d = root(tmp_path) / "conf"
    d.chmod(0o555)
    try:
        with pytest.raises(AccessError, match="another user.*PUID"):
            srv.write_config_file("one", HOT, "x")
    finally:
        d.chmod(0o755)
    assert (d / "hot.txt").read_text() == "a\n"
