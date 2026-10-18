"""
Digest tests against the tick engine (issue #1 §3.7 — the digest is a
stage keyed by local date: once/day, skipped if more than 4h late).
"""
import datetime as dtmod
from datetime import timedelta

import event_store
from occurrence import at_local, at_utc
from tests.conftest import local_dt, make_med, make_tick_env, TZ, utc

import pytest

TUE = dtmod.date(2026, 4, 21)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_tick_env(tmp_path, monkeypatch)


def digest_msgs(env):
    return [m for m in env.sent() if "Meds for" in m]


def test_digest_sent_at_first_tick_after_digest_time(env):
    env.write(meds=[make_med(med_id="med-001", frequency="once_daily",
                             times=["08:00"])],
              digest_time="07:30")
    env.run(local_dt(7, 0))
    env.run(local_dt(7, 32))
    msgs = digest_msgs(env)
    assert len(msgs) == 1
    assert "Metformin 500mg | 8:00am" in msgs[0]


def test_digest_once_per_local_date(env):
    env.write(meds=[make_med(med_id="med-001", frequency="once_daily",
                             times=["08:00"])],
              digest_time="07:30")
    env.run(local_dt(7, 0))
    env.run(local_dt(7, 32))
    env.run(local_dt(9, 0))
    env.run(local_dt(11, 0))
    assert len(digest_msgs(env)) == 1


def test_digest_skipped_when_more_than_4h_late(env):
    env.write(meds=[make_med(med_id="med-001", frequency="once_daily",
                             times=["20:00"])],
              digest_time="08:00")
    env.run(local_dt(7, 0))
    env.run(local_dt(12, 30))               # 4.5h after digest_time
    assert digest_msgs(env) == []


def test_digest_lists_active_meds_and_excludes_paused(env):
    met = make_med(med_id="med-001", name="Metformin", frequency="once_daily",
                   times=["08:00"])
    lis = make_med(med_id="med-002", name="Lisinopril", paused=True,
                   frequency="once_daily", times=["21:00"])
    env.write(meds=[met, lis], digest_time="07:30")
    env.run(local_dt(7, 0))
    env.run(local_dt(7, 32))
    msg = digest_msgs(env)[0]
    assert "Metformin" in msg and "Lisinopril" not in msg


def test_as_needed_med_shown_in_digest(env):
    env.write(meds=[make_med(med_id="med-001", name="Ibuprofen",
                             frequency="as_needed")],
              digest_time="07:30")
    env.run(local_dt(7, 0))
    env.run(local_dt(7, 32))
    msg = digest_msgs(env)[0]
    assert "Ibuprofen" in msg and "as needed" in msg


def test_digest_lists_weekly_day(env):
    env.write(meds=[make_med(med_id="med-001", frequency="weekly",
                             times=["09:00"], day_of_week="mon")],
              digest_time="07:30")
    env.run(local_dt(7, 0))
    env.run(local_dt(7, 32))
    assert "Mon 9:00am" in digest_msgs(env)[0]


def test_digest_adherence_from_history_store(env):
    # 3 taken + 1 missed in the last 7 days; one taken-overrides-missed pair
    med = make_med(med_id="med-001", frequency="once_daily", times=["08:00"])
    env.write(meds=[med], digest_time="07:30")
    evs = []
    for days_ago in (1, 2, 6):
        day = TUE - dtmod.timedelta(days=days_ago)
        occ = at_local(day, "08:00", TZ, 0)
        evs.append(event_store.new_event(
            "med-001", "taken", local_dt(8, 10) - dtmod.timedelta(days=days_ago),
            occurrence=occ, taken_at_utc=utc(local_dt(8, 10) - dtmod.timedelta(days=days_ago)).isoformat()))
    day3 = TUE - dtmod.timedelta(days=3)
    occ3 = at_local(day3, "08:00", TZ, 0)
    evs.append(event_store.new_event("med-001", "missed", local_dt(9, 30) - dtmod.timedelta(days=3), occurrence=occ3))
    evs.append(event_store.new_event("med-001", "taken", local_dt(10, 0) - dtmod.timedelta(days=3),
                                     occurrence=occ3))     # late taken overrides
    evs.append(event_store.new_event("med-001", "missed", local_dt(9, 30) - dtmod.timedelta(days=5),
                                     occurrence=at_local(TUE - dtmod.timedelta(days=5), "08:00", TZ, 0)))
    env.seed(evs)
    env.run(local_dt(7, 0))
    env.run(local_dt(7, 32))
    msg = digest_msgs(env)[0]
    # taken late counts as taken: 4 taken of 5 (the 5-days-ago miss is
    # outside the 7-day window anchored at the 07:32 digest… 5 days is
    # inside; 6-days-ago taken is inside too)
    assert "Last 7 days: 4/5 doses taken" in msg


def test_digest_adherence_omitted_without_events(env):
    env.write(meds=[make_med(med_id="med-001", frequency="once_daily",
                             times=["08:00"])],
              digest_time="07:30")
    env.run(local_dt(7, 0))
    env.run(local_dt(7, 32))
    assert "Last 7 days" not in digest_msgs(env)[0]


def test_globally_paused_skips_digest(env):
    env.write(meds=[make_med(med_id="med-001", frequency="once_daily",
                             times=["08:00"])],
              digest_time="07:30", paused=True)
    env.run(local_dt(7, 0))
    env.run(local_dt(7, 32))
    assert digest_msgs(env) == []
    assert env.sent() == []


def test_empty_meds_no_digest(env):
    env.write(meds=[], digest_time="07:30")
    env.run(local_dt(7, 0))
    env.run(local_dt(7, 32))
    assert env.sent() == []
