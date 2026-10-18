"""
Weekly-med tests — the helpers that survive PR 2 (most_recent_weekly_time,
compute_next_due) plus the setup-tasks registration shape. The tick-side
weekly behavior (day filter, Sunday-23:30 offsets landing Monday) lives in
tests/test_dst.py and tests/test_tick.py.

Fixture date 2026-04-21 is a Tuesday; Monday is 2026-04-20.
"""
from datetime import timedelta

from dispatch import compute_next_due, most_recent_weekly_time
from tests.conftest import local_dt, make_med, load_setup_tasks, TZ

setup_tasks = load_setup_tasks()
build_tasks, hhmm_to_cron = setup_tasks.build_tasks, setup_tasks.hhmm_to_cron


def monday(hour=9, minute=0):
    """Monday 2026-04-20 at HH:MM."""
    return local_dt(hour, minute, year=2026, month=4, day=20)


def tuesday(hour=12):
    return local_dt(hour, 0, year=2026, month=4, day=21)


# ── most_recent_weekly_time ────────────────────────────────────────────────────

def test_most_recent_weekly_same_day():
    # Tuesday 12:00, dose Tuesdays 09:00 → today 09:00
    assert most_recent_weekly_time("tue", "09:00", TZ, tuesday(12)) == tuesday(9)


def test_most_recent_weekly_earlier_day():
    # Tuesday 12:00, dose Mondays 09:00 → yesterday 09:00
    assert most_recent_weekly_time("mon", "09:00", TZ, tuesday(12)) == monday(9)


def test_most_recent_weekly_before_time_same_day():
    # Tuesday 07:00, dose Tuesdays 09:00 → last week's Tuesday 09:00
    expected = local_dt(9, 0, year=2026, month=4, day=14)  # Tue Apr 14 09:00
    assert most_recent_weekly_time("tue", "09:00", TZ, local_dt(7, 0, year=2026, month=4, day=21)) \
        == expected


# ── compute_next_due respects the day ──────────────────────────────────────────

def test_next_due_from_off_day_is_next_weeks_dose_day():
    med = make_med(frequency="weekly", times=["09:00"], day_of_week="mon")
    nd = compute_next_due(med, tuesday(12), TZ)
    assert nd == monday(9) + timedelta(days=7)


def test_next_due_on_dose_day_after_time_is_next_week():
    med = make_med(frequency="weekly", times=["09:00"], day_of_week="mon")
    nd = compute_next_due(med, monday(10), TZ)   # Monday 10:00, dose was 09:00
    assert nd == monday(9) + timedelta(days=7)


# ── scheduler registration (unchanged until PR 3) ─────────────────────────────

def test_build_tasks_weekly_carries_day():
    from tests.conftest import make_state
    med = make_med(med_id="med-001", frequency="weekly", times=["09:00"], day_of_week="mon")
    tasks = build_tasks(make_state(meds=[med]))
    dose_tasks = [t for t in tasks if t.name.startswith(("fire_", "check_", "miss_"))]
    assert len(dose_tasks) == 3
    assert all(t.day_of_week == "mon" for t in dose_tasks)


def test_cron_field_for_weekly():
    assert hhmm_to_cron("09:00", "mon") == "0 9 * * 1"
    assert hhmm_to_cron("09:00", "sun") == "0 9 * * 0"
    assert hhmm_to_cron("08:30") == "30 8 * * *"
