import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gamedock.backends import FakeBackend
from gamedock.errors import BackendError, NotFoundError, ValidationError
from gamedock.metrics import render_metrics
from gamedock.models import ContainerStats
from gamedock.sampler import Sampler
from gamedock.service import GameDockService
from gamedock.web.app import create_app
from tests.conftest import ADMIN, SAMPLE


class Clock:
    def __init__(self):
        self.t = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def backend(clock):
    b = FakeBackend()
    b.clock = clock
    return b


@pytest.fixture
def svc(backend, catalog_dir, tmp_path, clock):
    return GameDockService(backend, catalog_dir, tmp_path / "data", clock=clock,
                           stats_interval=10, stats_history_seconds=100)  # ring holds 10 points


def deploy(svc, name, port):
    from gamedock.models import PortMapping
    return svc.deploy("sample", name, ports=[PortMapping("udp", port, 27015)], env=ADMIN).id


def pin(backend, server_id, **kw):
    backend.stats_override[f"gamedock-{server_id}"] = ContainerStats(running=True, **kw)


# ------------------------------------------------------------------ snapshot contents
def test_stopped_and_running_servers(svc, backend, clock):
    a, b = deploy(svc, "Alpha", 27015), deploy(svc, "Beta", 27016)
    svc.start(a)
    clock.advance(90)
    pin(backend, a, started_at=clock() - timedelta(seconds=90), cpu_percent=200.0, mem_used=2 * 1024**3,
        mem_limit=8 * 1024**3, net_rx=1000, net_tx=2000, block_read=5, block_write=6, pids=7)
    snap = svc.sample_stats()
    by = {s.server_id: s for s in snap.servers}
    assert by[a].running and by[a].cpu_percent == 200.0 and by[a].uptime_seconds == 90.0
    assert by[a].cpu_of_host_percent == round(200.0 / snap.host.cpu_count, 2)
    assert by[a].mem_used == 2 * 1024**3 and by[a].pids == 7
    assert by[a].mem_of_host_percent == pytest.approx(100 * 2 * 1024**3 / snap.host.mem_total, abs=0.01)
    assert not by[b].running and by[b].cpu_percent == 0 and by[b].uptime_seconds is None
    assert snap.error == "" and snap.host.disk_total > 0


def test_network_rate_from_counter_deltas_and_restart_reset(svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    pin(backend, a, net_rx=1_000, net_tx=10_000)
    svc.sample_stats()
    clock.advance(10)
    pin(backend, a, net_rx=6_000, net_tx=30_000)
    s = svc.sample_stats().servers[0]
    assert s.net_rx_rate == 500.0 and s.net_tx_rate == 2000.0
    clock.advance(10)
    pin(backend, a, net_rx=100, net_tx=100)  # counters went backwards: container restarted
    s = svc.sample_stats().servers[0]
    assert s.net_rx_rate == 0.0 and s.net_tx_rate == 0.0


def test_docker_error_is_reported_not_raised(svc, backend):
    a = deploy(svc, "Alpha", 27015)
    backend.fail_next = "stats"
    snap = svc.sample_stats()
    assert "simulated failure" in snap.error
    assert snap.servers[0].running is False


# ------------------------------------------------------------------ cost control
def test_no_engine_call_without_servers(svc, backend):
    snap = svc.sample_stats()
    assert snap.servers == () and not [c for c in backend.calls if c[0] == "stats"]


def test_one_engine_call_per_sample_regardless_of_server_count(svc, backend):
    ids = [deploy(svc, f"S{i}", 27015 + i) for i in range(6)]
    for i in ids[:4]:
        svc.start(i)
    backend.calls.clear()
    svc.sample_stats()
    stats_calls = [c for c in backend.calls if c[0] == "stats"]
    assert len(stats_calls) == 1
    assert len(stats_calls[0][1].split(",")) == 6  # all names in a single call
    assert not [c for c in backend.calls if c[0] == "stop" or c[0] == "start"]


def test_history_only_recorded_while_something_runs(svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    for _ in range(3):
        clock.advance(10)
        svc.sample_stats()
    assert svc.get_stats_history(a, 100).points == ()
    assert svc.get_all_stats_history(100)[1] == []
    svc.start(a)
    clock.advance(10)
    svc.sample_stats()
    assert len(svc.get_stats_history(a, 100).points) == 1
    assert len(svc.get_all_stats_history(100)[1]) == 1


# ------------------------------------------------------------------ ring buffer
def test_ring_buffer_is_capped(svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    for _ in range(50):
        clock.advance(10)
        svc.sample_stats()
    assert svc._history_len == 10
    assert len(svc._history[a]) == 10 and len(svc._host_history) == 10
    pts = svc.get_stats_history(a, 100).points
    assert len(pts) == 10 and pts[0].t < pts[-1].t  # oldest first, newest kept


def test_ring_cap_has_a_hard_maximum(backend, catalog_dir, tmp_path):
    s = GameDockService(backend, catalog_dir, tmp_path / "d", stats_interval=2, stats_history_seconds=86400)
    assert s._history_len == 2000  # 43200 would be needed; capped


def test_memory_is_bounded_with_many_samples(svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    for _ in range(300):
        clock.advance(10)
        svc.sample_stats()
    assert len(svc._history[a]) <= svc._history_len


def test_history_window_filter_and_validation(svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    for _ in range(10):
        clock.advance(10)
        svc.sample_stats()
    assert len(svc.get_stats_history(a, 35).points) == 4  # samples 0..30 s old
    assert svc.get_stats_history(a, 10_000).window == 100  # capped at what is kept
    with pytest.raises(ValidationError):
        svc.get_stats_history(a, 3)
    with pytest.raises(NotFoundError):
        svc.get_stats_history("nope")


def test_deleted_server_history_is_dropped(svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    svc.sample_stats()
    assert a in svc._history
    svc.stop(a)
    svc.delete(a, confirm=True)
    svc.sample_stats()
    assert a not in svc._history


# ------------------------------------------------------------------ freshness / on-demand
def test_get_all_stats_uses_cache_when_fresh_and_reads_when_stale(svc, backend, clock):
    deploy(svc, "Alpha", 27015)
    svc.sample_stats()
    backend.calls.clear()
    svc.get_all_stats()
    assert backend.calls == []  # cached
    clock.advance(31)  # older than 3 x interval
    svc.get_all_stats()
    assert [c[0] for c in backend.calls] == ["stats"]


def test_on_demand_read_does_not_touch_history(svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    clock.advance(60)
    svc.get_all_stats()
    assert svc.get_stats_history(a, 100).points == ()


def test_sorting_and_unknown_sort(svc, backend, clock):
    a, b, c = deploy(svc, "Aaa", 27015), deploy(svc, "Bbb", 27016), deploy(svc, "Ccc", 27017)
    for i in (a, b, c):
        svc.start(i)
    pin(backend, a, cpu_percent=10, mem_used=300)
    pin(backend, b, cpu_percent=90, mem_used=100)
    pin(backend, c, cpu_percent=50, mem_used=900)
    svc.sample_stats()
    assert [s.server_id for s in svc.get_all_stats("cpu").servers] == [b, c, a]
    assert [s.server_id for s in svc.get_all_stats("memory").servers] == [c, a, b]
    assert [s.server_id for s in svc.get_all_stats("name").servers] == [a, b, c]
    with pytest.raises(ValidationError):
        svc.get_all_stats("bogus")


def test_get_server_stats_and_host(svc, backend):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    assert svc.get_server_stats(a).running
    with pytest.raises(NotFoundError):
        svc.get_server_stats("nope")
    h = svc.get_host_stats()
    assert h.cpu_count >= 1 and h.disk_free > 0


# ------------------------------------------------------------------ sampler thread
def test_sampler_thread_samples_and_stops(svc, backend):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    sampler = Sampler(svc, interval=0.05)
    sampler.start()
    deadline = time.time() + 3
    while time.time() < deadline and len([c for c in backend.calls if c[0] == "stats"]) < 3:
        time.sleep(0.05)
    sampler.stop()
    n = len([c for c in backend.calls if c[0] == "stats"])
    assert n >= 3
    time.sleep(0.2)
    assert len([c for c in backend.calls if c[0] == "stats"]) == n  # really stopped
    assert not any(t.name == "gamedock-sampler" for t in threading.enumerate())


def test_sampler_survives_errors(svc, backend):
    deploy(svc, "Alpha", 27015)
    calls = []
    orig = svc.sample_stats

    def flaky(record=True):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return orig(record)

    svc.sample_stats = flaky
    s = Sampler(svc, interval=0.05)
    s.start()
    deadline = time.time() + 3
    while time.time() < deadline and len(calls) < 3:
        time.sleep(0.05)
    s.stop()
    assert len(calls) >= 3


# ------------------------------------------------------------------ prometheus
def test_metrics_text_format(svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    b = deploy(svc, 'Be"ta\\', 27016)
    svc.start(a)
    pin(backend, a, started_at=clock(), cpu_percent=12.5, mem_used=1000, mem_limit=2000, net_rx=7, net_tx=9)
    text = render_metrics(svc.sample_stats())
    assert '# TYPE gamedock_server_cpu_percent gauge' in text
    assert f'gamedock_server_cpu_percent{{server="{a}",game="sample"}} 12.5' in text
    assert f'gamedock_server_memory_bytes{{server="{a}",game="sample"}} 1000' in text
    assert f'gamedock_server_running{{server="{a}",game="sample"}} 1' in text
    assert f'gamedock_server_running{{server="{b}",game="sample"}} 0' in text
    assert f'gamedock_server_network_receive_bytes_total{{server="{a}",game="sample"}} 7' in text
    assert "# TYPE gamedock_server_network_receive_bytes_total counter" in text
    assert "gamedock_servers 2" in text and "gamedock_host_memory_total_bytes" in text
    assert "gamedock_server_uptime_seconds" in text
    assert f'gamedock_server_uptime_seconds{{server="{b}"' not in text  # not running: no uptime series
    assert text.endswith("\n")
    for line in text.splitlines():  # every sample line is "name{labels} value"
        if not line.startswith("#"):
            assert len(line.rsplit(" ", 1)) == 2 and float(line.rsplit(" ", 1)[1]) is not None


def test_metrics_label_escaping():
    from gamedock.metrics import _esc
    assert _esc('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


# ------------------------------------------------------------------ API
@pytest.fixture
def client(svc):
    return TestClient(create_app(svc))


@pytest.fixture
def running_one(client, svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    client.post(f"/api/servers/{a}/start")
    pin(backend, a, started_at=clock(), cpu_percent=40, mem_used=1024**3, mem_limit=8 * 1024**3, net_rx=10, net_tx=20)
    for _ in range(3):
        clock.advance(10)
        svc.sample_stats()
    return a


def test_stats_endpoints(client, running_one):
    a = running_one
    r = client.get("/api/stats").json()
    assert {"sampled_at", "interval", "host", "servers"} <= set(r)
    assert r["servers"][0]["server_id"] == a and r["servers"][0]["cpu_percent"] == 40
    assert r["host"]["cpu_count"] >= 1
    assert client.get("/api/stats?sort=memory").status_code == 200
    assert client.get("/api/stats?sort=bogus").status_code == 400
    assert client.get("/api/stats/host").json()["disk_free"] > 0
    s = client.get(f"/api/servers/{a}/stats").json()
    assert s["running"] and s["mem_used"] == 1024**3
    assert client.get("/api/servers/nope/stats").status_code == 404


def test_history_endpoints(client, running_one):
    a = running_one
    h = client.get(f"/api/servers/{a}/stats/history?window=600").json()
    assert h["server_id"] == a and len(h["points"]) == 3
    assert set(h["points"][0]) == {"t", "cpu", "mem", "rx_rate", "tx_rate"}
    assert client.get(f"/api/servers/{a}/stats/history?window=1").status_code == 400
    allh = client.get("/api/stats/history?window=600").json()
    assert len(allh["servers"][a]["points"]) == 3 and len(allh["host"]) == 3
    assert client.get("/api/servers/nope/stats/history").status_code == 404


def test_metrics_disabled_by_default(client, running_one):
    assert client.get("/metrics").status_code == 404


def _metrics_client(svc, **kw):
    return TestClient(create_app(svc, metrics_enabled=True, **kw))


def test_metrics_enabled_serves_prometheus_text_to_loopback(svc, running_one):
    c = _metrics_client(svc)
    r = c.get("/metrics", headers={"host": "127.0.0.1:8080"})
    # TestClient's peer is "testclient", not loopback: refused unless remote is allowed
    assert r.status_code == 403
    c2 = TestClient(create_app(svc, metrics_enabled=True), client=("127.0.0.1", 50000))
    r2 = c2.get("/metrics")
    assert r2.status_code == 200 and r2.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "gamedock_server_cpu_percent" in r2.text
    c3 = TestClient(create_app(svc, metrics_enabled=True), client=("::1", 50000))
    assert c3.get("/metrics").status_code == 200


def test_metrics_remote_needs_explicit_setting(svc, running_one):
    remote = TestClient(create_app(svc, metrics_enabled=True), client=("192.168.1.50", 50000))
    assert remote.get("/metrics").status_code == 403
    allowed = TestClient(create_app(svc, metrics_enabled=True, metrics_allow_remote=True), client=("192.168.1.50", 50000))
    assert allowed.get("/metrics").status_code == 200


def test_metrics_does_not_query_docker_each_scrape(svc, running_one, backend):
    c = TestClient(create_app(svc, metrics_enabled=True), client=("127.0.0.1", 1))
    backend.calls.clear()
    for _ in range(5):
        assert c.get("/metrics").status_code == 200
    assert backend.calls == []  # served from the sampler's snapshot


def test_settings_from_env(monkeypatch):
    from gamedock.config import Settings
    assert Settings().metrics_enabled is False and Settings().stats_interval == 10.0
    monkeypatch.setenv("GAMEDOCK_STATS_INTERVAL", "0.5")
    monkeypatch.setenv("GAMEDOCK_STATS_HISTORY_SECONDS", "999999")
    monkeypatch.setenv("GAMEDOCK_METRICS_ENABLED", "true")
    monkeypatch.setenv("GAMEDOCK_STATS_ENABLED", "no")
    s = Settings.from_env()
    assert s.stats_interval == 2.0 and s.stats_history_seconds == 86400
    assert s.metrics_enabled is True and s.stats_enabled is False and s.metrics_allow_remote is False


def test_sampler_runs_with_the_app_lifespan(svc, backend):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    sampler = Sampler(svc, interval=0.05)
    with TestClient(create_app(svc, sampler=sampler)) as c:
        deadline = time.time() + 3
        while time.time() < deadline and not [x for x in backend.calls if x[0] == "stats"]:
            time.sleep(0.05)
        assert [x for x in backend.calls if x[0] == "stats"]
        assert c.get("/api/health").status_code == 200
    assert not any(t.name == "gamedock-sampler" for t in threading.enumerate())


def test_state_changes_invalidate_cached_stats(svc, backend, clock):
    a = deploy(svc, "Alpha", 27015)
    svc.start(a)
    svc.sample_stats()
    assert svc.get_all_stats().servers[0].running
    svc.stop(a)  # no clock advance: without invalidation the cached "running" snapshot would be returned
    assert not svc.get_all_stats().servers[0].running
