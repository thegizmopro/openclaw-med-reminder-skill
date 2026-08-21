"""
Weekly-med tests — regression for the bug where weekly meds were scheduled
and fired DAILY because day-of-week wasn't in the schema.

Fixture date 2026-04-21 is a Tuesday; Monday is 2026-04-20.
"""
from datetime import timedelta
from unittest.mock import patch

from dispatch import (
    covered_dose_occurrence,
    compute_next_due,
    dose_time_for,
    handle_digest,
    most_recent_weekly_time,
    skip_if_needed,
)
from tests.conftest import make_state, make_med, local_dt, iso, fake_now, load_setup_tasks, TZ

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


# ── skip_if_needed day guard ───────────────────────────────────────────────────

def test_fire_skips_on_wrong_weekday():
    med = make_med(frequency="weekly", times=["09:00"], day_of_week="mon")
    state = make_state(meds=[med])
    dose_time = dose_time_for(med, 0, TZ, tuesday(12))
    assert skip_if_needed(state, med, dose_time, "fire", TZ, tuesday(12)) is True

def test_fire_proceeds_on_dose_weekday():
    med = make_med(frequency="weekly", times=["09:00"], day_of_week="mon")
    state = make_state(meds=[med])
    now = monday(9)  # exactly dose time
    dose_time = dose_time_for(med, 0, TZ, now)
    assert dose_time == monday(9)
    assert skip_if_needed(state, med, dose_time, "fire", TZ, now) is False

def test_confirmed_dose_skips_on_dose_weekday():
    med = make_med(frequency="weekly", times=["09:00"], day_of_week="mon",
                   status="confirmed", last_taken=iso(monday(9, 14)),
                   confirmed_dose=iso(monday(9)))
    state = make_state(meds=[med])
    now = monday(9) + timedelta(minutes=30)
    assert skip_if_needed(state, med, dose_time_for(med, 0, TZ, now), "check", TZ, now) is True


# ── covered_dose_occurrence anchored to the weekday ────────────────────────────

def test_covered_weekly_confirm_next_day_credits_dose_day():
    # User confirms Wednesday for a Monday med → credits Monday's occurrence
    med = make_med(frequency="weekly", times=["09:00"], day_of_week="mon")
    wed = local_dt(12, 0, year=2026, month=4, day=22)
    assert covered_dose_occurrence(med, wed, TZ) == monday(9)


# ── scheduler registration ─────────────────────────────────────────────────────

def test_build_tasks_weekly_carries_day():
    med = make_med(med_id="med-001", frequency="weekly", times=["09:00"], day_of_week="mon")
    tasks = build_tasks(make_state(meds=[med]))
    dose_tasks = [t for t in tasks if t.name.startswith(("fire_", "check_", "miss_"))]
    assert len(dose_tasks) == 3
    assert all(t.day_of_week == "mon" for t in dose_tasks)

def test_cron_field_for_weekly():
    assert hhmm_to_cron("09:00", "mon") == "0 9 * * 1"
    assert hhmm_to_cron("09:00", "sun") == "0 9 * * 0"
    assert hhmm_to_cron("08:30") == "30 8 * * *"


# ── digest shows the day ───────────────────────────────────────────────────────

def test_digest_lists_weekly_day():
    med = make_med(med_id="med-001", frequency="weekly", times=["09:00"], day_of_week="mon")
    state = make_state(meds=[med])
    sent = []
    with patch("dispatch.load_state", return_value=state), \
         patch("dispatch.save_state"), \
         patch("dispatch.send_message", side_effect=lambda t, dr: sent.append(t)), \
         patch("dispatch.datetime", fake_now(tuesday(12))):
        handle_digest(dry_run=False)
    assert any("Mon 9:00am" in t for t in sent)
