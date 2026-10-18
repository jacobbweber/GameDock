from pathlib import Path

import pytest

from gamedock.catalog import load_catalog, parse_definition
from gamedock.errors import CatalogError


def test_shipped_catalog_loads():
    defs = load_catalog(Path(__file__).parent.parent / "catalog")
    assert {"enshrouded", "palworld", "valheim", "minecraft-java", "satisfactory"} <= set(defs)
    assert defs["enshrouded"].ports[0].protocol == "udp"
    for e in defs.values():
        assert e.notes, f"{e.id} should carry an 'verify before use' note"
        for f in e.fields:
            if "PASS" in f.name:
                assert f.secret, f"{e.id}.{f.name} should be secret"


def test_entry_roundtrip():
    from gamedock.catalog import entry_to_dict
    for e in load_catalog(Path(__file__).parent.parent / "catalog").values():
        assert parse_definition(entry_to_dict(e)) == e


def test_missing_catalog_dir_is_empty(tmp_path):
    assert load_catalog(tmp_path / "nope") == {}


@pytest.mark.parametrize(
    "data",
    [
        {"name": "x", "image": "y"},
        {"id": "Bad Id", "name": "x", "image": "y"},
        {"id": "a", "name": "x", "image": "y", "ports": [{"protocol": "icmp", "host": 1, "container": 1}]},
        {"id": "a", "name": "x", "image": "y", "ports": [{"protocol": "tcp", "host": 70000, "container": 1}]},
        {"id": "a", "name": "x", "image": "y", "volumes": [{"host": "../etc", "container": "/x"}]},
        {"id": "a", "name": "x", "image": "y", "fields": [{"name": "bad name"}]},
        {"id": "a", "name": "x", "image": "y", "env": {"A": "1"}, "fields": [{"name": "A"}]},
        {"id": "a", "name": "x", "image": "y",
         "ports": [{"protocol": "udp", "host": 1, "container": 5}, {"protocol": "udp", "host": 2, "container": 5}]},
        "not a mapping",
    ],
)
def test_invalid_definitions_rejected(data):
    with pytest.raises(CatalogError):
        parse_definition(data)


def test_duplicate_ids_rejected(tmp_path):
    for n in ("a.yaml", "b.yaml"):
        (tmp_path / n).write_text("id: same\nname: n\nimage: i\n")
    with pytest.raises(CatalogError):
        load_catalog(tmp_path)


# ------------------------------------------------------------------ review pass: safer definitions
BASE = {"id": "g", "name": "G", "image": "busybox"}


@pytest.mark.parametrize("patch,msg", [
    ({"image": "--privileged"}, "not a valid image name"),
    ({"image": "a b"}, "not a valid image name"),
    ({"env": {"PATH": "/x"}}, "reserved"),
    ({"env": {"DOCKER_HOST": "tcp://x"}}, "reserved"),
    ({"env": {"A": "line\nbreak"}}, "line breaks"),
    ({"fields": [{"name": "LD_PRELOAD"}]}, "reserved"),
    ({"volumes": [{"host": "./d", "container": "relative/path"}]}, "must be absolute"),
    ({"volumes": [{"host": "./d", "container": "/a/../b"}]}, "must be absolute"),
])
def test_unsafe_definitions_are_rejected(patch, msg):
    with pytest.raises(CatalogError, match=msg):
        parse_definition({**BASE, **patch})


def test_good_definition_with_registry_image():
    e = parse_definition({**BASE, "image": "ghcr.io/org/game:1.2",
                          "volumes": [{"host": "./d", "container": "/data"}], "env": {"TZ": "UTC"}})
    assert e.image == "ghcr.io/org/game:1.2"


def test_load_catalog_can_collect_problems_instead_of_raising(tmp_path):
    (tmp_path / "ok.yaml").write_text("id: ok\nname: Ok\nimage: busybox\n")
    (tmp_path / "bad.yaml").write_text("id: bad\nname: Bad\n")
    with pytest.raises(CatalogError):
        load_catalog(tmp_path)
    problems = []
    assert list(load_catalog(tmp_path, errors=problems)) == ["ok"]
    assert problems and problems[0][0] == "bad.yaml"
