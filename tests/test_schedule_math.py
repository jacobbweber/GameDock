from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from gamedock import schedule as sc
from gamedock.errors import ValidationError

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def local(dt, tz=NY):
    return dt.astimezone(tz)


def test_interval_is_anchor_plus_hours():
    rec = sc.Recurrence("interval", every_hours=6)
    assert sc.next_run(rec, utc(2026, 3, 1, 10, 30), NY) == utc(2026, 3, 1, 16, 30)


def test_daily_before_and_after_the_time():
    rec = sc.Recurrence("daily", time="03:00")
    # 01:00 New York time -> same day 03:00; 04:00 -> next day 03:00
    assert local(sc.next_run(rec, datetime(2026, 5, 10, 1, 0, tzinfo=NY), NY)) == datetime(2026, 5, 10, 3, 0, tzinfo=NY)
    assert local(sc.next_run(rec, datetime(2026, 5, 10, 4, 0, tzinfo=NY), NY)) == datetime(2026, 5, 11, 3, 0, tzinfo=NY)


def test_daily_is_strictly_after_the_anchor():
    rec = sc.Recurrence("daily", time="03:00")
    at = datetime(2026, 5, 10, 3, 0, tzinfo=NY)
    assert local(sc.next_run(rec, at, NY)) == datetime(2026, 5, 11, 3, 0, tzinfo=NY)


def test_weekly_picks_the_next_chosen_day():
    rec = sc.Recurrence("weekly", time="04:00", days=("sun", "wed"))
    fri = datetime(2026, 5, 8, 12, 0, tzinfo=NY)       # 2026-05-08 is a Friday
    assert local(sc.next_run(rec, fri, NY)) == datetime(2026, 5, 10, 4, 0, tzinfo=NY)   # Sunday
    sun_after = datetime(2026, 5, 10, 5, 0, tzinfo=NY)
    assert local(sc.next_run(rec, sun_after, NY)) == datetime(2026, 5, 13, 4, 0, tzinfo=NY)  # Wednesday


def test_weekly_single_day_same_day_passed_goes_a_week_on():
    rec = sc.Recurrence("weekly", time="04:00", days=("sun",))
    sun = datetime(2026, 5, 10, 4, 30, tzinfo=NY)
    assert local(sc.next_run(rec, sun, NY)) == datetime(2026, 5, 17, 4, 0, tzinfo=NY)


def test_dst_spring_forward_gap_runs_right_after_the_jump():
    # 2026-03-08: clocks jump 02:00 -> 03:00 in New York, so 02:30 does not exist that day
    rec = sc.Recurrence("daily", time="02:30")
    n = sc.next_run(rec, datetime(2026, 3, 7, 12, 0, tzinfo=NY), NY)
    assert local(n) == datetime(2026, 3, 8, 3, 30, tzinfo=NY)
    # next day it is 02:30 again
    n2 = sc.next_run(rec, n, NY)
    assert local(n2) == datetime(2026, 3, 9, 2, 30, tzinfo=NY)


def test_dst_fall_back_repeated_time_runs_once():
    # 2026-11-01: 01:00-02:00 happens twice in New York
    rec = sc.Recurrence("daily", time="01:30")
    first = sc.next_run(rec, datetime(2026, 10, 31, 12, 0, tzinfo=NY), NY)
    assert first == utc(2026, 11, 1, 5, 30)             # first 01:30 (still summer time, UTC-4)
    second = sc.next_run(rec, first, NY)
    assert local(second) == datetime(2026, 11, 2, 1, 30, tzinfo=NY)   # the repeated 01:30 is not used


def test_daily_keeps_local_time_across_dst_change():
    rec = sc.Recurrence("daily", time="03:00")
    before = sc.next_run(rec, datetime(2026, 3, 7, 12, 0, tzinfo=NY), NY)
    after = sc.next_run(rec, before, NY)
    assert before == utc(2026, 3, 8, 7, 0)     # 03:00 EDT after the jump is already UTC-4
    assert after == utc(2026, 3, 9, 7, 0)
    assert sc.next_run(rec, datetime(2026, 3, 6, 12, 0, tzinfo=NY), NY) == utc(2026, 3, 7, 8, 0)  # still UTC-5


def test_midnight_and_late_evening_edges():
    rec = sc.Recurrence("daily", time="00:00")
    assert local(sc.next_run(rec, datetime(2026, 5, 10, 23, 59, tzinfo=NY), NY)) == datetime(2026, 5, 11, 0, 0, tzinfo=NY)
    rec = sc.Recurrence("daily", time="23:59")
    assert local(sc.next_run(rec, datetime(2026, 5, 10, 23, 59, tzinfo=NY), NY)) == datetime(2026, 5, 11, 23, 59, tzinfo=NY)


def test_system_timezone_when_none_given():
    rec = sc.Recurrence("daily", time="03:00")
    n = sc.next_run(rec, utc(2026, 5, 10, 0, 0), None)
    assert n > utc(2026, 5, 10, 0, 0) and n - utc(2026, 5, 10, 0, 0) <= timedelta(hours=49)


def test_describe_texts():
    assert sc.describe(sc.Recurrence("interval", every_hours=6)) == "every 6 hours"
    assert sc.describe(sc.Recurrence("interval", every_hours=1)) == "every hour"
    assert sc.describe(sc.Recurrence("daily", time="03:00")) == "daily 03:00"
    assert sc.describe(sc.Recurrence("weekly", time="04:00", days=("sun",))) == "Sun 04:00"
    assert sc.describe(sc.Recurrence("weekly", time="04:00", days=("mon", "thu"))) == "Mon, Thu 04:00"
    assert sc.describe(sc.Recurrence("weekly", time="04:00", days=sc.DAY_NAMES)) == "daily 04:00"


@pytest.mark.parametrize("bad", ["3:00", "24:00", "12:60", "", "03:00:00", "ab:cd", None, 300, "03:5"])
def test_bad_times_are_refused(bad):
    with pytest.raises(ValidationError):
        sc.parse_time(bad)


def test_validate_recurrence_strict():
    ok = sc.validate_recurrence("weekly", None, "04:00", ["Sun", "mon", "sun"], sc.BACKUP_KINDS)
    assert ok.days == ("mon", "sun") and ok.time == "04:00"
    for args in [("hourly", None, None, None), ("interval", 0, None, None), ("interval", 169, None, None),
                 ("interval", "6", None, None), ("interval", True, None, None), ("interval", 1.5, None, None),
                 ("weekly", None, "04:00", []), ("weekly", None, "04:00", ["funday"]), ("weekly", None, "04:00", "sun"),
                 ("daily", None, "7pm", None)]:
        with pytest.raises(ValidationError):
            sc.validate_recurrence(*args, sc.BACKUP_KINDS)
    with pytest.raises(ValidationError):   # maintenance has no "every N hours"
        sc.validate_recurrence("interval", 6, None, None, sc.MAINTENANCE_KINDS)


def test_unused_fields_are_dropped():
    r = sc.validate_recurrence("daily", 99, "03:00", ["mon"], sc.BACKUP_KINDS)
    assert r == sc.Recurrence("daily", 0, "03:00", ())


def test_retention_bounds():
    assert sc.validate_retention(None) is None and sc.validate_retention(5) == 5
    for bad in (0, 101, -1, "5", True, 2.5):
        with pytest.raises(ValidationError):
            sc.validate_retention(bad)


def test_next_due_uses_last_run_else_since():
    rule = sc.Recurrence("interval", every_hours=2)
    st = sc.RunState(since="2026-01-01T00:00:00Z")
    assert sc.next_due(rule, st, NY) == utc(2026, 1, 1, 2, 0)
    st = sc.RunState(since="2026-01-01T00:00:00Z", last_run="2026-01-01T05:00:00Z")
    assert sc.next_due(rule, st, NY) == utc(2026, 1, 1, 7, 0)
    assert sc.next_due(rule, sc.RunState(), NY) is None


def test_host_timezone_returns_a_name():
    tz, name = sc.host_timezone()
    assert isinstance(name, str) and name
