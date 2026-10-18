from datetime import datetime, timezone

import pytest

from gamedock.backends import DockerCliBackend
from gamedock.backends.docker_stats import (
    parse_docker_time, parse_inspect_lines, parse_pair, parse_percent, parse_size, parse_stats_lines,
)
from gamedock.errors import BackendError
from gamedock.hoststats import HostMeter, parse_meminfo, parse_proc_stat
from tests.test_docker_cli import Recorder


@pytest.mark.parametrize("text,expected", [
    ("1.23%", 1.23), ("0.00%", 0.0), ("250.5%", 250.5), ("--", 0.0), ("", 0.0), (" 12% ", 12.0),
])
def test_parse_percent(text, expected):
    assert parse_percent(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("0B", 0), ("512B", 512), ("1.2kB", 1200), ("3.4MB", 3_400_000), ("2GB", 2_000_000_000),
    ("512MiB", 512 * 1024**2), ("8GiB", 8 * 1024**3), ("1.5GiB", int(1.5 * 1024**3)), ("64KiB", 65536),
    ("--", 0), ("", 0), ("12", 12),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_parse_pair():
    assert parse_pair("512MiB / 8GiB") == (512 * 1024**2, 8 * 1024**3)
    assert parse_pair("1.2kB / 3.4MB") == (1200, 3_400_000)
    assert parse_pair("0B / 0B") == (0, 0)
    assert parse_pair("garbage") == (0, 0)


STATS_OUTPUT = (
    '{"BlockIO":"12.3MB / 4.56MB","CPUPerc":"1.23%","Container":"abc","ID":"abc123","MemPerc":"6.25%",'
    '"MemUsage":"512MiB / 8GiB","Name":"gamedock-one","NetIO":"1.2kB / 3.4MB","PIDs":"27"}\n'
    '{"BlockIO":"0B / 0B","CPUPerc":"250.50%","Container":"def","ID":"def456","MemPerc":"50%",'
    '"MemUsage":"4.5GiB / 8GiB","Name":"gamedock-two","NetIO":"0B / 0B","PIDs":"--"}\n'
    "\nnot json\n"
)


def test_parse_stats_lines():
    s = parse_stats_lines(STATS_OUTPUT)
    assert set(s) == {"gamedock-one", "gamedock-two"}
    one = s["gamedock-one"]
    assert one.running and one.cpu_percent == 1.23 and one.pids == 27
    assert one.mem_used == 512 * 1024**2 and one.mem_limit == 8 * 1024**3
    assert one.net_rx == 1200 and one.net_tx == 3_400_000
    assert one.block_read == 12_300_000 and one.block_write == 4_560_000
    two = s["gamedock-two"]
    assert two.cpu_percent == 250.5 and two.mem_used == int(4.5 * 1024**3) and two.pids == 0


def test_parse_stats_ignores_garbage():
    assert parse_stats_lines("") == {}
    assert parse_stats_lines('{"Name": ""}\n{broken') == {}


def test_parse_docker_time():
    t = parse_docker_time("2026-10-17T21:00:00.123456789Z")
    assert t == datetime(2026, 10, 17, 21, 0, 0, 123456, tzinfo=timezone.utc)
    assert parse_docker_time("2026-10-17T21:00:00Z") == datetime(2026, 10, 17, 21, 0, tzinfo=timezone.utc)
    assert parse_docker_time("2026-10-17T23:00:00+02:00") == datetime(2026, 10, 17, 21, 0, tzinfo=timezone.utc)
    assert parse_docker_time("0001-01-01T00:00:00Z") is None
    assert parse_docker_time("nonsense") is None


INSPECT_OUTPUT = (
    "/gamedock-one|running|2026-10-17T21:00:00.5Z|0\n"
    "/gamedock-two|exited|2026-10-16T10:00:00Z|2147483648\n"
)


def test_parse_inspect_lines():
    r = parse_inspect_lines(INSPECT_OUTPUT)
    assert r["gamedock-one"][0] == "running" and r["gamedock-one"][2] == 0
    assert r["gamedock-two"] == ("exited", datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc), 2147483648)


# ---- the Docker backend makes at most two calls, whatever the number of servers
def test_backend_two_calls_for_many_servers():
    inspect = ("/gamedock-one|running|2026-10-17T21:00:00Z|0\n/gamedock-two|exited|2026-10-17T20:00:00Z|0\n"
               "/gamedock-three|running|2026-10-17T22:00:00Z|1073741824\n")
    stats = (STATS_OUTPUT.splitlines()[0] + "\n" +
             '{"CPUPerc":"5%","MemUsage":"1GiB / 8GiB","Name":"gamedock-three","NetIO":"0B / 0B","BlockIO":"0B / 0B","PIDs":"3"}\n')
    r = Recorder([(0, inspect, ""), (0, stats, "")])
    out = DockerCliBackend(runner=r).get_stats(["gamedock-one", "gamedock-two", "gamedock-three"])
    assert len(r.calls) == 2
    assert r.calls[0][0][:3] == ["docker", "container", "inspect"] and r.calls[1][0][:3] == ["docker", "stats", "--no-stream"]
    assert "gamedock-two" not in r.calls[1][0]  # stopped containers are not passed to `docker stats`
    assert out["gamedock-one"].running and out["gamedock-one"].started_at.year == 2026
    assert not out["gamedock-two"].running
    assert out["gamedock-three"].mem_limit == 1073741824  # configured limit wins


def test_backend_no_stats_call_when_nothing_runs():
    r = Recorder([(0, "/gamedock-one|exited|2026-10-17T21:00:00Z|0\n", "")])
    out = DockerCliBackend(runner=r).get_stats(["gamedock-one"])
    assert len(r.calls) == 1 and not out["gamedock-one"].running


def test_backend_no_calls_without_servers():
    r = Recorder([])
    assert DockerCliBackend(runner=r).get_stats([]) == {} and r.calls == []


def test_backend_missing_containers_are_left_out():
    r = Recorder([(1, "", "Error: No such object: gamedock-x")])
    assert DockerCliBackend(runner=r).get_stats(["gamedock-x"]) == {}


def test_backend_real_inspect_failure_raises():
    r = Recorder([(1, "", "Cannot connect to the Docker daemon")])
    with pytest.raises(BackendError):
        DockerCliBackend(runner=r).get_stats(["gamedock-x"])


# ---- host stats
PROC_STAT = "cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 500 0 250 4000 250 0 0 0 0 0\n"
MEMINFO = "MemTotal:       16384000 kB\nMemFree:         1000000 kB\nMemAvailable:    8192000 kB\nBuffers: 5 kB\n"


def test_parse_proc_stat_and_meminfo():
    assert parse_proc_stat(PROC_STAT) == (10000, 8500)
    assert parse_proc_stat("nothing") is None
    m = parse_meminfo(MEMINFO)
    assert m["MemTotal"] == 16384000 * 1024 and m["MemAvailable"] == 8192000 * 1024


def test_host_meter_reads_real_system(tmp_path):
    h = HostMeter().read(tmp_path / "does" / "not" / "exist")  # falls back to an existing parent
    assert h.cpu_count >= 1 and h.disk_total > 0 and h.disk_free <= h.disk_total
    assert h.cpu_percent is None or 0 <= h.cpu_percent <= 100
    assert h.mem_total is None or h.mem_used <= h.mem_total


def test_host_meter_cpu_is_delta_between_reads(monkeypatch, tmp_path):
    import gamedock.hoststats as hs
    texts = iter(["cpu  100 0 0 900 0 0 0 0\n", "cpu  200 0 0 1000 0 0 0 0\n", "cpu  400 0 0 1100 0 0 0 0\n"])
    monkeypatch.setattr(hs, "_read", lambda p: next(texts) if p == "/proc/stat" else None)
    monkeypatch.setattr(hs.time, "sleep", lambda s: None)
    m = HostMeter()
    assert m._cpu() == 50.0       # 100 busy of 200 between the two initial reads
    assert m._cpu() == 66.7       # 200 busy of 300
