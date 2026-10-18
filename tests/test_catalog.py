from pathlib import Path

import pytest

from gamedock.catalog import load_catalog, parse_definition
from gamedock.errors import CatalogError


def test_shipped_catalog_loads():
    defs = load_catalog(Path(__file__).parent.parent / "catalog")
    assert "enshrouded" in defs
    assert defs["enshrouded"].ports[0].protocol == "udp"


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
