"""0.6.0: inspect_image "tag not found" (available tags, plain reasons, saved inspection record) and the extra
validate_catalog_entry warnings (never inspected, latest after a failed inspect, unmapped env/ports/volumes, ready
pattern, steam app id, too few fields, default-port collisions)."""
import json
import textwrap

import pytest
import yaml

from gamedock import authoring as au
from gamedock.backends import docker_errors as de
from gamedock.errors import ValidationError
from tests.mcp_helpers import call, make_app, run
from tests.test_authoring import GOOD, good

IMG = "example/test-game-server:1.2"


def hub(tags, status=200):
    seen = []

    def get(url, timeout):
        seen.append(url)
        return status, json.dumps({"results": [{"name": t} for t in tags]})
    get.seen = seen
    return get


def facts(**kw):
    base = {"exposed_ports": ["27015/udp", "27016/tcp"], "volumes": ["/saves"], "user": "1000",
            "env_names": ["SERVER_NAME", "SERVER_PASSWORD", "MODE", "PATH", "HOME"], "healthcheck": None}
    base.update(kw)
    return base


def stored(backend, image=IMG, **kw):
    backend.local_images[image] = "sha256:abc"
    backend.image_facts[image] = facts(**kw)
    backend.remote_facts[image] = {"digest": "sha256:" + "a" * 64, "platforms": ["linux/amd64"]}


def warnings(service, text=GOOD):
    return " || ".join(service.validate_catalog_entry(text)["warnings"])


# ------------------------------------------------------------------ registry failure wording
@pytest.mark.parametrize("text,kind", [
    ("manifest unknown: manifest unknown", "not_found"), ("Error: no such manifest: docker.io/x/y:9", "not_found"),
    ("pull access denied for x/y, repository does not exist or may require authorization", "not_found"),
    ("unauthorized: authentication required", "denied"),
    ("toomanyrequests: You have reached your pull rate limit", "rate_limit"),
    ("dial tcp: lookup registry-1.docker.io: no such host", "offline"), ("i/o timeout", "offline"),
    ("something odd", "other")])
def test_classify_registry_error(text, kind):
    assert de.classify_registry_error(text) == kind


def test_registry_failure_sentences_are_plain():
    nf = de.registry_failure("not_found", "x/y:9")
    assert "x/y:9" in nf and "tag" in nf and "does not exist" in nf
    assert "(no internet)" in de.registry_failure("offline", "x/y:1")
    assert "rate" in de.registry_failure("rate_limit", "x/y:1").lower()
    assert "sign in" in de.registry_failure("denied", "x/y:1").lower() or "private" in de.registry_failure("denied", "x/y:1").lower()


# ------------------------------------------------------------------ inspect_image
def test_tag_not_found_lists_the_tags_that_exist(service, backend):
    service.steam.enabled = True
    service.http_get = hub(["1.1.0", "1.2.0", "latest-dev", "2.0"])
    r = service.inspect_image("example/test-game-server:1.2")
    assert r["ok"] is False and r["tag_status"] == "not_found" and r["available_tags"] == ["1.1.0", "1.2.0", "latest-dev", "2.0"]
    assert "1.2.0" in r["similar_tags"] and "does not exist" in r["failure_reason"]
    text = " ".join(r["summary"])
    assert "Tags that exist for this image" in text and "Pick one of these exact tags" in text
    assert "hub.docker.com/v2/repositories/example/test-game-server/tags" in service.http_get.seen[0]
    assert not [c for c in backend.calls if c[0] in ("pull", "create", "start")]


def test_official_image_uses_library_namespace(service):
    service.steam.enabled = True
    service.http_get = hub(["1", "1.36"])
    service.inspect_image("busybox:99")
    assert "repositories/library/busybox/tags" in service.http_get.seen[0]


def test_tag_lookup_failures_do_not_break_the_inspection(service):
    service.steam.enabled = True

    def boom(url, timeout):
        raise OSError("offline")
    service.http_get = boom
    r = service.inspect_image("example/x:1")
    assert r["ok"] is False and r["available_tags"] == [] and "could not be fetched" in r["tag_listing"]
    service.http_get = hub([], status=404)
    assert "no repository" in service.inspect_image("example/x:1")["tag_listing"]
    service.http_get = hub([], status=500)
    assert "HTTP 500" in service.inspect_image("example/x:1")["tag_listing"]
    service.http_get = lambda u, t: (200, "not json")
    assert "could not be read" in service.inspect_image("example/x:1")["tag_listing"]


def test_other_registries_and_digests_are_not_asked(service):
    service.steam.enabled = True
    service.http_get = hub(["a"])
    r = service.inspect_image("ghcr.io/someone/thing:1")
    assert "only available for Docker Hub" in r["tag_listing"] and service.http_get.seen == []
    r = service.inspect_image("example/x@sha256:" + "a" * 64)
    assert r["tag_status"] == "unknown" and service.http_get.seen == []


def test_tag_lookup_is_off_when_update_checks_are_off(service):
    service.http_get = hub(["a"])
    r = service.inspect_image("example/x:1")
    assert "switched off" in r["tag_listing"] and service.http_get.seen == []


def test_offline_and_found_images_report_the_right_state(service, backend):
    backend.offline = True
    r = service.inspect_image("example/x:1")
    assert r["ok"] is False and r["tag_status"] == "unknown" and "(no internet)" in r["failure_reason"]
    backend.offline = False
    stored(backend)
    r = service.inspect_image(IMG)
    assert r["ok"] and r["tag_status"] == "found" and r["failure_reason"] == ""


def test_inspection_record_is_saved_without_secrets_or_values(service, backend):
    stored(backend, env_names=["SERVER_NAME", "TOKEN_X"])
    service.inspect_image(IMG)
    raw = (service.data_dir / "inspected-images.json").read_text()
    assert "TOKEN_X" in raw and "sha256:abc" not in raw
    assert oct((service.data_dir / "inspected-images.json").stat().st_mode & 0o777) == "0o600"
    store = au.InspectionStore(service.data_dir / "inspected-images.json")
    assert store.get(IMG)["ok"] is True and store.get("busybox:1") is None


def test_inspection_store_keeps_at_most_a_hundred(tmp_path):
    s = au.InspectionStore(tmp_path / "i.json")
    for i in range(130):
        s.record(f"example/img:{i}", {"ok": True, "local": False})
    assert len(json.loads((tmp_path / "i.json").read_text())) == 100
    assert s.get("example/img:129") is not None and s.get("example/img:0") is None


def test_inspect_image_tool_returns_the_new_fields(service):
    err, r = run(make_app(service, "read_only"), lambda c: call(c, "inspect_image", image="example/x:1"))
    assert not err and {"ok", "tag_status", "available_tags", "failure_reason"} <= set(r)


def test_inspect_still_refuses_odd_references(service):
    with pytest.raises(ValidationError):
        service.inspect_image("bad image")


# ------------------------------------------------------------------ validate: image facts
def test_never_inspected_warning_then_clears(service, backend):
    assert "was never inspected" in warnings(service)
    stored(backend)
    service.inspect_image(IMG)
    assert "never inspected" not in warnings(service)


def test_failed_inspect_with_latest_tag_warns(service):
    text = good(image="example/test-game-server:latest")
    service.inspect_image("example/test-game-server:latest")
    w = warnings(service, text)
    assert "could not find" in w and "'latest'" in w and "many images do not publish" in w
    service.inspect_image(IMG)
    assert "could not confirm" in warnings(service)


def test_registry_only_inspection_is_an_info_note(service, backend):
    backend.remote_facts[IMG] = {"digest": "sha256:" + "a" * 64, "platforms": ["linux/amd64"]}
    service.inspect_image(IMG)
    r = service.validate_catalog_entry(GOOD)
    assert "never inspected" not in " ".join(r["warnings"]) and any("only checked at the registry" in n for n in r["notes"])
    assert r["image_inspection"] == {"inspected": True, "at": r["image_inspection"]["at"], "ok": True, "stored_here": False}


def test_unmapped_env_ports_and_volumes_are_reported(service, backend):
    stored(backend, env_names=["SERVER_NAME", "SERVER_PASSWORD", "MODE", "MAX_PLAYERS", "PATH", "HOME", "LANG"],
           exposed_ports=["27015/udp", "27016/tcp", "8080/tcp", "9000"], volumes=["/saves", "/data/extra"])
    service.inspect_image(IMG)
    w = warnings(service)
    assert "MAX_PLAYERS" in w and "PATH" not in w.split("MAX_PLAYERS")[0].split("environment variables")[-1]
    assert "8080/tcp" in w and "9000" in w and "/data/extra" in w
    assert "/saves," not in w and "/saves." not in w     # covered by the mounted ./saves folder


def test_fully_mapped_image_has_no_image_warnings(service, backend):
    stored(backend)
    service.inspect_image(IMG)
    w = warnings(service)
    for bad in ("environment variables that the entry", "declares these ports", "declares these volumes", "never inspected"):
        assert bad not in w


def test_no_ready_pattern_and_no_healthcheck(service, backend):
    stored(backend)
    service.inspect_image(IMG)
    text = good(ready_log_pattern=None)
    assert "the image has no health check" in warnings(service, text)
    backend.image_facts[IMG]["healthcheck"] = {"test": ["CMD", "true"]}
    service.inspect_image(IMG)
    assert "the image has no health check" not in warnings(service, text)


# ------------------------------------------------------------------ validate: entry-only warnings
def test_few_fields_warning_and_the_notes_exception(service):
    d = yaml.safe_load(GOOD)
    d["fields"] = d["fields"][:1]
    assert "Only 1 setting(s) are exposed" in warnings(service, yaml.safe_dump(d, sort_keys=False))
    d["notes"] = "Other settings were deliberately left out because they are advanced."
    assert "are exposed as fields" not in warnings(service, yaml.safe_dump(d, sort_keys=False))


def test_steam_app_id_derivability(service):
    d = yaml.safe_load(GOOD)
    r = service.validate_catalog_entry(yaml.safe_dump(d, sort_keys=False))
    assert any("appmanifest_123456.acf" in n for n in r["notes"]) and not any("no folder is mounted" in w for w in r["warnings"])
    d["volumes"] = []
    r = service.validate_catalog_entry(yaml.safe_dump(d, sort_keys=False))
    assert any("no folder is mounted" in w and "version tracking cannot work" in w for w in r["warnings"])
    d["installed_build_file"] = "build.txt"
    r = service.validate_catalog_entry(yaml.safe_dump(d, sort_keys=False))
    assert not any("no folder is mounted" in w for w in r["warnings"])
    d.pop("steam_app_id")
    assert any("No steam_app_id" in n for n in service.validate_catalog_entry(yaml.safe_dump(d, sort_keys=False))["notes"])


def test_default_port_collision_with_another_entry_is_a_warning(service):
    # the sample catalog entry already uses 27015/udp by default
    d = yaml.safe_load(GOOD)
    d["ports"][0]["host"] = 27015
    w = warnings(service, yaml.safe_dump(d, sort_keys=False))
    assert "27015" in w and "Sample Game" in w and "for example 27016" in w
    assert "27015" not in warnings(service, GOOD)


def test_validate_stays_read_only_and_warnings_do_not_block_add(service, backend):
    before = list(backend.calls)
    service.validate_catalog_entry(GOOD)
    assert backend.calls == before and not (service.data_dir / "inspected-images.json").exists()
    assert service.add_catalog_entry(GOOD)["status"] == "added"
