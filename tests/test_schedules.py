import json
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from gamedock import schedule as sc
from gamedock import service as svc_mod
from gamedock.backends import FakeBackend
from gamedock.errors import BusyError, ConflictError, ValidationError
from gamedock.models import PortMapping, Status
from gamedock.service import GameDockService
from tests.conftest import ADMIN, SAMPLE

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
IMAGE = "busybox:latest"

STOPPER = textwrap.dedent("""\
    id: stopper
    name: Stopper Game
    image: stopper:1
    update_note: Game files update when the container starts.
    backup_stop_first: true
    volumes:
      - { host: ./data, container: /data }
    fields:
      - { name: ADMIN_PASSWORD, secret: true, required: true }
    """)
SAVER = textwrap.dedent("""\
    id: saver
    name: Saver Game
    image: saver:1
    save_command: [rcon-cli, save]
    volumes:
      - { host: ./data, container: /data }
    fields:
      - { name: ADMIN_PASSWORD, secret: true, required: true }
    """)
SELFUPDATING = textwrap.dedent("""\
    id: selfup
    name: Self Updating
    image: selfup:1
    update_method: none
    volumes:
      - { host: ./data, container: /data }
    """)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC))


@pytest.fixture(autouse=True)
def no_save_pause(monkeypatch):
    monkeypatch.setattr(svc_mod, "SAVE_COMMAND_PAUSE", 0)


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def service(backend, tmp_path, clock):
    cat = tmp_path / "catalog"
    cat.mkdir()
    for name, text in (("sample", SAMPLE), ("stopper", STOPPER), ("saver", SAVER), ("selfup", SELFUPDATING)):
        (cat / f"{name}.yaml").write_text(text)
    return GameDockService(backend, cat, tmp_path / "data", clock=clock, tz=NY, tz_name="America/New_York")


def make(service, game="sample", name="One", start=False):
    env = ADMIN if game in ("sample",) else {"ADMIN_PASSWORD": "pw"} if game != "selfup" else {}
    ports = [PortMapping("udp", 27100 + len(service.store.all()), 27015)] if game == "sample" else None
    sid = service.deploy(game, name, ports=ports, env=env).id
    (service.data_dir / sid / ("saves" if game == "sample" else "data")).mkdir(parents=True, exist_ok=True)
    (service.data_dir / sid / ("saves" if game == "sample" else "data") / "world.dat").write_text("world")
    if start:
        service.start(sid)
    return sid


def inline(fn):
    fn()


def tick(service, now=None):
    return service.process_schedules(now, spawn=inline)


# ------------------------------------------------------------------ setting schedules
def test_defaults_are_off(service):
    sid = make(service)
    v = service.get_server_schedules(sid)
    assert v["backup"]["enabled"] is False and v["maintenance"]["enabled"] is False
    assert v["maintenance"]["only_when_empty"]["available"] is False
    assert v["timezone"]["name"] == "America/New_York" and v["timezone"]["utc_offset"] == "UTC-04:00"
    assert service.get_status(sid).schedule["next_run"] is None


def test_set_backup_schedule_and_next_run(service):
    sid = make(service)
    v = service.set_backup_schedule(sid, True, "daily", time_of_day="03:00", retention=5)
    b = v["backup"]
    assert b["enabled"] and b["summary"] == "daily at 03:00" and b["retention"] == 5 and b["retention_in_use"] == 5
    assert b["next_run"] == "2026-05-11T07:00:00Z"        # 03:00 New York time (UTC-4)
    ov = service.get_status(sid).schedule
    assert ov["backup"] == "daily at 03:00" and ov["maintenance"] == "" and ov["next_kind"] == "backup"


def test_first_enable_needs_a_frequency(service):
    sid = make(service)
    with pytest.raises(ValidationError, match="how often"):
        service.set_backup_schedule(sid, True)
    with pytest.raises(ValidationError, match="when to run"):
        service.set_maintenance_schedule(sid, True)


def test_switching_off_keeps_settings_and_back_on_restarts_the_clock(service, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=6)
    service.set_backup_schedule(sid, False)
    b = service.get_server_schedules(sid)["backup"]
    assert not b["enabled"] and b["every_hours"] == 6 and b["next_run"] is None
    clock.advance(hours=30)
    b = service.set_backup_schedule(sid, True)["backup"]            # no frequency: reuse the stored one
    assert b["enabled"] and b["every_hours"] == 6
    assert b["next_run"] == sc.iso(clock.t + timedelta(hours=6))    # counted from now, not from before


def test_validation_is_strict(service):
    sid = make(service)
    bad = [dict(frequency="hourly"), dict(frequency="interval", every_hours=0), dict(frequency="interval", every_hours=200),
           dict(frequency="daily", time_of_day="25:00"), dict(frequency="weekly", time_of_day="03:00", days=[]),
           dict(frequency="weekly", time_of_day="03:00", days=["noday"]),
           dict(frequency="daily", time_of_day="03:00", retention=0), dict(frequency="daily", time_of_day="03:00", retention=1000)]
    for kw in bad:
        with pytest.raises(ValidationError):
            service.set_backup_schedule(sid, True, **kw)
    with pytest.raises(ValidationError):
        service.set_backup_schedule(sid, "yes", "daily", time_of_day="03:00")
    with pytest.raises(ValidationError):
        service.set_maintenance_schedule(sid, True, "interval", time_of_day="03:00")
    with pytest.raises(ValidationError, match="at least one thing"):
        service.set_maintenance_schedule(sid, True, "daily", "04:00", backup_first=False, update_image=False, restart=False)
    assert service.get_server_schedules(sid)["backup"]["enabled"] is False     # nothing half-saved


def test_image_update_option_refused_for_self_updating_games(service):
    sid = make(service, "selfup", "Self")
    with pytest.raises(ValidationError, match="no image update"):
        service.set_maintenance_schedule(sid, True, "weekly", "04:00", ["sun"], update_image=True)
    v = service.set_maintenance_schedule(sid, True, "weekly", "04:00", ["sun"], update_image=False)
    assert v["maintenance"]["image_update_supported"] is False and v["maintenance"]["summary"] == "Sunday at 04:00"


def test_unknown_server(service):
    from gamedock.errors import NotFoundError
    with pytest.raises(NotFoundError):
        service.set_backup_schedule("nope", False)


def test_overview_picks_the_earlier_job(service):
    sid = make(service)
    service.set_backup_schedule(sid, True, "daily", time_of_day="03:00")
    service.set_maintenance_schedule(sid, True, "weekly", "04:00", ["mon", "sun"])
    ov = service.get_status(sid).schedule
    assert ov["next_kind"] == "backup" and ov["maintenance"] == "Monday, Sunday at 04:00"


# ------------------------------------------------------------------ persistence
def test_schedules_survive_a_restart_in_a_consistent_file(service, backend, tmp_path, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "weekly", time_of_day="03:15", days=["sat", "mon"], retention=3)
    service.set_maintenance_schedule(sid, True, "daily", "05:00", backup_first=False, update_image=True, restart=True)
    raw = json.loads((service.data_dir / "schedules.json").read_text())
    assert raw["version"] == 1
    b = raw["servers"][sid]["backup"]
    assert b["enabled"] and b["kind"] == "weekly" and b["time"] == "03:15" and b["days"] == ["mon", "sat"] and b["retention"] == 3
    assert raw["servers"][sid]["maintenance"]["backup_first"] is False
    again = GameDockService(backend, service.catalog_dir, service.data_dir, clock=clock, tz=NY, tz_name="x")
    assert again.get_server_schedules(sid)["backup"]["days"] == ["mon", "sat"]
    assert again.get_server_schedules(sid)["maintenance"]["summary"] == "daily at 05:00"
    assert oct((service.data_dir / "schedules.json").stat().st_mode & 0o777) == "0o600"
    assert not list(service.data_dir.glob(".schedules-*"))      # no temporary files left behind


def test_corrupt_file_is_set_aside_not_fatal(service, backend, tmp_path, clock):
    sid = make(service)
    (service.data_dir / "schedules.json").write_text("{ this is not json")
    fresh = GameDockService(backend, service.catalog_dir, service.data_dir, clock=clock, tz=NY, tz_name="x")
    assert fresh.get_server_schedules(sid)["backup"]["enabled"] is False
    assert "could not be read" in fresh.list_schedules()["problem"]
    assert list(service.data_dir.glob("schedules.json.broken-*"))
    fresh.set_backup_schedule(sid, True, "daily", time_of_day="03:00")      # and it works again
    assert json.loads((service.data_dir / "schedules.json").read_text())["servers"][sid]["backup"]["enabled"]


def test_one_invalid_entry_does_not_hide_the_others(service, backend, clock):
    a = make(service, name="A")
    b = make(service, name="B")
    service.set_backup_schedule(a, True, "daily", time_of_day="03:00")
    service.set_backup_schedule(b, True, "daily", time_of_day="04:00")
    p = service.data_dir / "schedules.json"
    raw = json.loads(p.read_text())
    raw["servers"][a]["backup"]["time"] = "99:99"
    p.write_text(json.dumps(raw))
    fresh = GameDockService(backend, service.catalog_dir, service.data_dir, clock=clock, tz=NY, tz_name="x")
    assert fresh.get_server_schedules(b)["backup"]["summary"] == "daily at 04:00"
    assert fresh.get_server_schedules(a)["backup"]["enabled"] is False
    assert fresh.schedules.problem


def test_deleting_a_server_removes_its_schedules(service):
    sid = make(service)
    service.set_backup_schedule(sid, True, "daily", time_of_day="03:00")
    service.delete(sid, confirm=True)
    assert sid not in json.loads((service.data_dir / "schedules.json").read_text())["servers"]


# ------------------------------------------------------------------ running due jobs
def test_nothing_runs_before_it_is_due_then_runs_once(service, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=2)
    clock.advance(hours=1, minutes=59)
    assert tick(service) == 0
    clock.advance(minutes=2)
    assert tick(service) == 1
    b = service.get_server_schedules(sid)["backup"]
    assert b["last_result"] == "ok" and "Saved backup" in b["last_message"]
    assert b["next_run"] == sc.iso(clock.t + timedelta(hours=2))
    assert tick(service) == 0                                      # not again at the next tick
    names = [x.name for x in service.list_backups(sid)]
    assert len(names) == 1 and "scheduled" in names[0]
    assert service.list_backups(sid)[0].kind == "scheduled"


def test_missed_runs_run_once_not_repeated(service, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    clock.advance(days=3)                                          # GameDock was off for three days
    assert tick(service) == 1
    assert tick(service) == 0
    clock.advance(minutes=1)
    assert tick(service) == 0
    assert len(service.list_backups(sid)) == 1
    h = service.get_server_schedules(sid)["history"]
    assert len(h) == 1 and h[0]["trigger"] == "catch-up"
    clock.advance(hours=1)
    assert tick(service) == 1
    assert service.get_server_schedules(sid)["history"][0]["trigger"] == "schedule"


def test_catch_up_after_restart_uses_stored_last_run(service, backend, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "daily", time_of_day="03:00")
    clock.t = datetime(2026, 5, 11, 7, 0, 30, tzinfo=UTC)          # 03:00:30 New York
    tick(service)
    assert len(service.list_backups(sid)) == 1
    clock.t = datetime(2026, 5, 13, 20, 0, 0, tzinfo=UTC)          # off for two days, back in the evening
    again = GameDockService(backend, service.catalog_dir, service.data_dir, clock=clock, tz=NY, tz_name="x")
    assert tick(again) == 1 and len(again.list_backups(sid)) == 2
    assert tick(again) == 0


def test_disabled_schedules_never_run(service, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    service.set_backup_schedule(sid, False)
    clock.advance(days=2)
    assert tick(service) == 0


def test_weekly_backup_on_the_right_day(service, clock):
    sid = make(service)
    # Sunday 2026-05-10 12:00 UTC is 08:00 New York; weekly on Wednesday 03:00
    service.set_backup_schedule(sid, True, "weekly", time_of_day="03:00", days=["wed"])
    clock.t = datetime(2026, 5, 13, 6, 59, tzinfo=UTC)
    assert tick(service) == 0
    clock.t = datetime(2026, 5, 13, 7, 0, 1, tzinfo=UTC)
    assert tick(service) == 1


def test_scheduled_backup_of_running_server_is_live_by_default(service, backend):
    sid = make(service, start=True)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    backend.calls.clear()
    service._clock.advance(hours=1, minutes=1)
    tick(service)
    assert "stop" not in [c[0] for c in backend.calls]
    assert service.get_status(sid).status == Status.RUNNING
    assert "NOT stopped" in service.get_server_schedules(sid)["backup"]["last_message"]


def test_backup_stop_first_flag_is_honoured(service, backend):
    sid = make(service, "stopper", "Stopper", start=True)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    assert "stopped briefly" in service.get_server_schedules(sid)["backup"]["how"]
    backend.calls.clear()
    service._clock.advance(hours=2)
    tick(service)
    actions = [c[0] for c in backend.calls]
    assert "stop" in actions and "start" in actions
    assert service.get_status(sid).status == Status.RUNNING
    assert service.get_server_schedules(sid)["backup"]["last_result"] == "ok"


def test_save_command_runs_before_a_live_backup(service, backend):
    sid = make(service, "saver", "Saver", start=True)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    service._clock.advance(hours=2)
    tick(service)
    assert backend.exec_log == [("gamedock-saver", ["rcon-cli", "save"])]
    assert "asked to save" in service.get_server_schedules(sid)["backup"]["last_message"]


def test_save_command_failure_is_a_note_and_the_backup_still_happens(service, backend):
    sid = make(service, "saver", "Saver", start=True)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    backend.fail_next = "exec"
    service._clock.advance(hours=2)
    tick(service)
    b = service.get_server_schedules(sid)["backup"]
    assert b["last_result"] == "ok" and "save command did not work" in b["last_message"]
    assert len(service.list_backups(sid)) == 1


def test_old_servers_pick_up_hints_from_the_current_catalog(service, backend):
    sid = make(service, "saver", "Saver", start=True)
    inst = service.store.get(sid)
    for k in ("update_note", "backup_stop_first", "save_command"):
        inst.template.pop(k, None)               # a server saved by version 0.2.0 has none of these
    service.store.put(inst)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    service._clock.advance(hours=2)
    tick(service)
    assert backend.exec_log, "the save command from the current catalog entry was used"


def test_retention_from_the_schedule_applies(service, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1, retention=2)
    for _ in range(4):
        clock.advance(hours=1, minutes=1)
        tick(service)
    assert len(service.list_backups(sid)) == 2
    assert service.get_server_schedules(sid)["backup"]["retention_in_use"] == 2


def test_backup_of_empty_server_is_skipped_not_failed(service, clock):
    sid = service.deploy("sample", "Empty", env=ADMIN).id
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    clock.advance(hours=2)
    tick(service)
    b = service.get_server_schedules(sid)["backup"]
    assert b["last_result"] == "skipped" and "Nothing to back up" in b["last_message"]


def test_failure_is_recorded_visibly_and_not_retried_in_a_loop(service, clock, caplog, monkeypatch):
    from gamedock.errors import OperationError
    sid = make(service, start=True)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)

    def boom(*a, **k):
        raise OperationError("Not enough free disk space to save the backup.")
    monkeypatch.setattr(svc_mod.bk, "create_archive", boom)
    clock.advance(hours=2)
    with caplog.at_level("ERROR"):
        assert tick(service) == 1
    v = service.get_server_schedules(sid)
    assert v["backup"]["last_result"] == "failed" and "free disk space" in v["backup"]["last_message"]
    assert v["overview"]["last_problem"] is True and v["history"][0]["result"] == "failed"
    assert any("Scheduled backup of" in r.message and "failed" in r.message for r in caplog.records)
    clock.advance(minutes=1)
    assert tick(service) == 0                                    # waits for the next scheduled time, no retry loop
    assert v["backup"]["next_run"] > sc.iso(clock.t)


def test_unexpected_crash_is_recorded(service, monkeypatch, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    monkeypatch.setattr(service, "_job_backup", lambda inst: 1 / 0)
    clock.advance(hours=2)
    tick(service)
    b = service.get_server_schedules(sid)["backup"]
    assert b["last_result"] == "failed" and "Unexpected error" in b["last_message"]
    assert service.get_status(sid).schedule["last_problem"] is True


def test_audit_entries_for_scheduled_runs(service, clock):
    records = []
    service.audit_hook = lambda tool, args, status, detail: records.append((tool, args, status, detail))
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    clock.advance(hours=2)
    tick(service)
    assert records and records[0][0] == "scheduled_backup" and records[0][2] == "ok"
    assert records[0][1]["server_id"] == sid and records[0][1]["trigger"] in ("schedule", "catch-up")


def test_audit_hook_failure_does_not_break_the_job(service, clock):
    def bad(*a):
        raise RuntimeError("disk full")
    service.audit_hook = bad
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    clock.advance(hours=2)
    tick(service)
    assert service.get_server_schedules(sid)["backup"]["last_result"] == "ok"


# ------------------------------------------------------------------ locking
def test_busy_server_waits_and_runs_later(service, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    clock.advance(hours=2)
    with service._operation(sid, "starting"):                    # the user is doing something
        assert tick(service) == 1
        assert service.get_server_schedules(sid)["backup"]["last_result"] is None   # nothing recorded
        assert not service.list_backups(sid)
    assert tick(service) == 1                                    # next tick: free, so it runs
    assert len(service.list_backups(sid)) == 1


def test_busy_for_two_hours_is_skipped(service, clock, monkeypatch):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    clock.advance(hours=2)
    t = [1000.0]
    monkeypatch.setattr(svc_mod.time, "monotonic", lambda: t[0])
    with service._operation(sid, "starting"):
        tick(service)
        t[0] += 3 * 3600
        tick(service)
    b = service.get_server_schedules(sid)["backup"]
    assert b["last_result"] == "skipped" and "two hours" in b["last_message"]


def test_a_job_is_not_started_twice(service, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    clock.advance(hours=2)
    spawned = []
    assert service.process_schedules(spawn=spawned.append) == 1
    assert service.process_schedules(spawn=spawned.append) == 0      # still counted as running
    spawned[0]()
    assert service.process_schedules(spawn=spawned.append) == 0      # done and rescheduled


def test_maintenance_blocks_other_actions_on_the_same_server(service, backend):
    sid = make(service, start=True)
    service.set_maintenance_schedule(sid, True, "daily", "04:00", update_image=False)
    backend.delays["stop"] = 0.6
    out = {}
    t = threading.Thread(target=lambda: out.update(r=service.run_schedule_now(sid, "maintenance", confirm=True)))
    t.start()
    time.sleep(0.2)
    with pytest.raises(BusyError):
        service.restart(sid)
    with pytest.raises(BusyError):
        service.create_backup(sid)
    assert service.get_status(sid).activity == "maintenance"
    t.join(5)
    assert out["r"]["result"] == "ok"
    service.restart(sid)                                          # free again


def test_frozen_service_refuses_work_and_ticks(service, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    clock.advance(hours=2)
    service.freeze("GameDock is updating")
    assert tick(service) == 0
    with pytest.raises(BusyError, match="updating"):
        service.start(sid)
    service.unfreeze()
    assert tick(service) == 1


# ------------------------------------------------------------------ maintenance
def actions(backend):
    return [c[0] for c in backend.calls]


def test_maintenance_restart_with_backup_and_no_update(service, backend):
    sid = make(service, start=True)
    service.set_maintenance_schedule(sid, True, "daily", "04:00", backup_first=True, update_image=True, restart=True)
    backend.calls.clear()
    r = service.run_schedule_now(sid, "maintenance", confirm=True)
    assert r["result"] == "ok"
    assert actions(backend) == ["pull", "stop", "create", "start"]    # pull while running, then one stop and one start
    assert service.get_status(sid).status == Status.RUNNING
    kinds = [b.kind for b in service.list_backups(sid)]
    assert kinds == ["pre-maintenance"]
    assert "already the newest" in r["message"] and "restarted" in r["message"]
    st = service.get_server_schedules(sid)["maintenance"]
    assert st["last_result"] == "ok" and st["last_run"]


def test_maintenance_applies_a_new_image(service, backend):
    sid = make(service, start=True)
    service.set_maintenance_schedule(sid, True, "daily", "04:00", backup_first=True, update_image=True, restart=True)
    before = service.get_image_info(sid).image_id
    backend.publish_new_image(IMAGE)
    backend.calls.clear()
    r = service.run_schedule_now(sid, "maintenance", confirm=True)
    assert r["result"] == "ok" and "new image" in r["message"]
    assert actions(backend) == ["pull", "stop", "remove", "create", "create", "start"]   # (the second create is a no-op check)
    assert service.get_image_info(sid).image_id != before
    assert len(service.list_backups(sid)) == 1                  # one backup, not two


def test_maintenance_update_only_takes_pre_update_backup(service, backend):
    sid = make(service, start=True)
    service.set_maintenance_schedule(sid, True, "daily", "04:00", backup_first=False, update_image=True, restart=False)
    backend.publish_new_image(IMAGE)
    service.run_schedule_now(sid, "maintenance", confirm=True)
    assert [b.kind for b in service.list_backups(sid)] == ["pre-update"]
    assert service.get_status(sid).status == Status.RUNNING


def test_maintenance_backup_only_on_running_server_is_live(service, backend):
    sid = make(service, start=True)
    service.set_maintenance_schedule(sid, True, "daily", "04:00", backup_first=True, update_image=False, restart=False)
    backend.calls.clear()
    service.run_schedule_now(sid, "maintenance", confirm=True)
    assert "stop" not in actions(backend)
    assert [b.kind for b in service.list_backups(sid)] == ["pre-maintenance"]


def test_maintenance_on_stopped_server_leaves_it_stopped(service, backend):
    sid = make(service)
    service.set_maintenance_schedule(sid, True, "daily", "04:00")
    backend.calls.clear()
    r = service.run_schedule_now(sid, "maintenance", confirm=True)
    assert "start" not in actions(backend) and "left stopped" in r["message"]
    assert service.get_status(sid).status != Status.RUNNING


def test_maintenance_pull_failure_still_does_the_rest_and_reports_failure(service, backend):
    sid = make(service, start=True)
    service.set_maintenance_schedule(sid, True, "daily", "04:00")
    backend.fail_next = "pull"
    r = service.run_schedule_now(sid, "maintenance", confirm=True)
    assert r["result"] == "failed" and "could not be checked" in r["message"] and "restarted" in r["message"]
    assert service.get_status(sid).status == Status.RUNNING
    assert service.get_server_schedules(sid)["history"][0]["result"] == "failed"


def test_maintenance_backup_failure_restarts_server_and_changes_nothing_else(service, backend, monkeypatch):
    sid = make(service, start=True)
    service.set_maintenance_schedule(sid, True, "daily", "04:00")
    backend.publish_new_image(IMAGE)
    before = service.get_image_info(sid).image_id
    from gamedock.errors import OperationError

    def boom(*a, **k):
        raise OperationError("disk full")
    monkeypatch.setattr(svc_mod.GameDockService, "_safety_backup", boom)
    r = service.run_schedule_now(sid, "maintenance", confirm=True)
    assert r["result"] == "failed" and "disk full" in r["message"]
    assert service.get_status(sid).status == Status.RUNNING          # not left down
    assert service.get_server_schedules(sid)["maintenance"]["last_result"] == "failed"
    assert backend.get_container_image_id("gamedock-" + sid) == before  # container was not recreated


def test_run_now_needs_confirmation_and_valid_kind(service):
    sid = make(service)
    with pytest.raises(ValidationError, match="confirmation"):
        service.run_schedule_now(sid, "maintenance")
    with pytest.raises(ValidationError):
        service.run_schedule_now(sid, "reboot", confirm=True)
    service.set_maintenance_schedule(sid, False, "daily", "04:00", backup_first=False, update_image=False, restart=False)
    with pytest.raises(ValidationError, match="nothing to do"):
        service.run_schedule_now(sid, "maintenance", confirm=True)


def test_run_now_backup(service):
    sid = make(service)
    r = service.run_schedule_now(sid, "backup", confirm=True)
    assert r["result"] == "ok" and len(service.list_backups(sid)) == 1
    assert service.get_server_schedules(sid)["history"][0]["trigger"] == "manual"


def test_run_now_on_busy_server_is_refused(service):
    sid = make(service)
    with service._operation(sid, "updating"):
        with pytest.raises(BusyError):
            service.run_schedule_now(sid, "backup", confirm=True)


def test_late_maintenance_is_skipped_but_backups_catch_up(service, clock):
    sid = make(service, start=True)
    service.set_maintenance_schedule(sid, True, "daily", "04:00")
    service.set_backup_schedule(sid, True, "daily", time_of_day="03:00")
    clock.t = datetime(2026, 5, 11, 20, 0, tzinfo=UTC)               # GameDock was off; it is 16:00 the next day
    assert tick(service) == 2
    m = service.get_server_schedules(sid)["maintenance"]
    assert m["last_result"] == "skipped" and "was not running" in m["last_message"]
    assert service.get_status(sid).status == Status.RUNNING          # nobody was kicked
    assert service.get_server_schedules(sid)["backup"]["last_result"] == "ok"
    assert m["next_run"] == "2026-05-12T08:00:00Z"


def test_slightly_late_maintenance_still_runs(service, clock, backend):
    sid = make(service, start=True)
    service.set_maintenance_schedule(sid, True, "daily", "04:00")
    clock.t = datetime(2026, 5, 11, 10, 0, tzinfo=UTC)               # 06:00 New York, two hours late
    tick(service)
    assert service.get_server_schedules(sid)["maintenance"]["last_result"] == "ok"
    assert service.get_server_schedules(sid)["history"][0]["trigger"] == "catch-up"


def test_history_is_capped_and_newest_first(service, clock):
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1, retention=3)
    for _ in range(sc.HISTORY_LIMIT + 5):
        clock.advance(hours=1, minutes=1)
        tick(service)
    h = service.get_server_schedules(sid)["history"]
    assert len(h) == sc.HISTORY_LIMIT and h[0]["time"] > h[-1]["time"]


# ------------------------------------------------------------------ image update indicator
def test_image_update_detection_cycle(service, backend):
    sid = make(service, start=True)
    assert service.get_status(sid).update_available is None       # never checked
    r = service.check_image_update(sid, force=True)
    assert r["update_available"] is False and "newest" in r["message"]
    assert service.get_status(sid).update_available is False
    backend.publish_new_image(IMAGE)
    service._image_checks[sid].checked_at -= 10 * 3600           # the cached answer is old now
    r = service.check_image_update(sid)
    assert r["update_available"] is True
    info = service.get_status(sid)
    assert info.update_available is True and info.update_checked_at
    service.update_server_image(sid, confirm=True)
    assert service.get_status(sid).update_available is False


def test_image_check_is_cached_for_hours_and_rate_limited(service, backend):
    sid = make(service)
    service.check_image_update(sid, force=True)
    n = len([c for c in backend.calls if c[0] == "remote_digest"])
    service.check_image_update(sid)                               # cached
    service.check_image_update(sid, force=True)                   # pressed again within 30 s
    assert len([c for c in backend.calls if c[0] == "remote_digest"]) == n
    service._image_checks[sid].checked_at -= 31
    service.check_image_update(sid, force=True)
    assert len([c for c in backend.calls if c[0] == "remote_digest"]) == n + 1


def test_image_check_offline_is_unknown_and_retried_sooner(service, backend):
    sid = make(service)
    backend.offline = True
    r = service.check_image_update(sid, force=True)
    assert r["update_available"] is None and r["failed"] and "no internet" in r["message"]
    assert service.get_status(sid).update_available is None
    backend.offline = False
    service._image_checks[sid].checked_at -= 31 * 60              # failed answers are only kept 30 minutes
    assert service.check_image_update(sid)["update_available"] is False


def test_image_check_unknown_when_registry_says_nothing(service, backend):
    sid = make(service)
    backend.registry.pop(IMAGE)
    r = service.check_image_update(sid, force=True)
    assert r["update_available"] is None and "did not report" in r["message"]


def test_image_check_unknown_for_locally_built_image(service, backend):
    sid = make(service)
    backend.local_images.clear()
    backend._container_image.clear()
    r = service.check_image_update(sid, force=True)
    assert r["update_available"] is None and "no registry record" in r["message"]


def test_games_that_update_themselves_have_no_image_check(service, backend):
    sid = make(service, "selfup", "Self")
    r = service.check_image_update(sid, force=True)
    assert r["supported"] is False and r["update_available"] is None
    assert not [c for c in backend.calls if c[0] == "remote_digest"]
    assert service.get_status(sid).update_available is None


def test_update_note_is_exposed(service):
    sid = make(service, "stopper", "Stopper")
    assert service.get_status(sid).update_note == "Game files update when the container starts."
    assert service.check_image_update(sid, force=True)["update_note"].startswith("Game files")


def test_background_check_asks_one_server_per_call(service, backend):
    for n in ("A", "B", "C"):
        make(service, name=n)
    assert service.check_image_updates_due(limit=1) == 1
    assert service.check_image_updates_due(limit=1) == 1
    assert service.check_image_updates_due(limit=5) == 1          # only C was left
    assert service.check_image_updates_due(limit=5) == 0          # all fresh: nothing to ask


def test_scheduler_run_once_does_everything_safely(service, clock, backend):
    from gamedock.scheduler import Scheduler
    sid = make(service)
    service.set_backup_schedule(sid, True, "interval", every_hours=1)
    clock.advance(hours=2)
    s = Scheduler(service, background_checks=True)
    s.run_once()
    time.sleep(0.3)                                               # the job runs in its own thread
    assert service.get_server_schedules(sid)["backup"]["last_result"] == "ok"
    assert service.get_status(sid).update_available is False
    service.process_schedules = lambda *a, **k: 1 / 0
    s.run_once()                                                  # a crashing tick is survived
