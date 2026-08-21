"""
Per-dose confirmation tests — regressions for two escalation bugs:

  #1 early confirm: replying 'all taken' before the dose fires (e.g. to the
     07:30 digest for an 08:00 dose) used to trigger the full
     reminder → nudge → missed escalation anyway
  #4 second dose unloggable: with status 'confirmed' left over from the morning
     dose (scheduled fire never ran — asleep machine, quiet hours), the evening
     confirm used to be refused

Both stem from per-med status/last_taken conflating doses; the fix anchors
confirms to the scheduled occurrence they credit (state.confirmed_dose).
"""
from datetime import timedelta
from unittest.mock import patch

from dispatch import (
    already_confirmed,
    covered_dose_occurrence,
    handle_fire,
    most_recent_dose_time,
    skip_if_needed,
)
from reply import handle_confirm
from tests.conftest import make_state, make_med, local_dt, iso, fake_now, TZ


def covered(med, now):
    return covered_dose_occurrence(med, now, TZ)


# ── covered_dose_occurrence: which occurrence a confirm credits ────────────────

def test_early_confirm_credits_upcoming_dose():
    # 'all taken' at 07:32 replying to the 07:30 digest, first dose at 08:00
    med = make_med(frequency="twice_daily", times=["08:00", "20:00"])
    assert covered(med, local_dt(7, 32)) == local_dt(8, 0)


def test_ontime_confirm_credits_that_dose():
    med = make_med(frequency="twice_daily", times=["08:00", "20:00"])
    assert covered(med, local_dt(8, 14)) == local_dt(8, 0)


def test_evening_confirm_credits_evening_dose():
    med = make_med(frequency="twice_daily", times=["08:00", "20:00"])
    assert covered(med, local_dt(20, 30)) == local_dt(20, 0)


def test_midday_confirm_credits_morning_dose():
    med = make_med(frequency="twice_daily", times=["08:00", "20:00"])
    assert covered(med, local_dt(12, 0)) == local_dt(8, 0)


def test_morning_confirm_does_not_credit_far_off_evening_dose():
    # 07:32 'all taken' must not credit the 21:00 dose — tonight still reminds
    med = make_med(frequency="once_daily", times=["21:00"])
    assert covered(med, local_dt(7, 32)) == local_dt(21, 0) - timedelta(days=1)


def test_weekly_confirm_credits_that_days_dose():
    med = make_med(frequency="weekly", times=["09:00"])
    assert covered(med, local_dt(9, 14)) == local_dt(9, 0)


def test_interval_and_as_needed_have_no_covered_occurrence():
    assert covered(make_med(frequency="interval", interval_hours=6),
                   local_dt(12, 0)) is None
    assert covered(make_med(frequency="as_needed"), local_dt(12, 0)) is None


# ── already_confirmed: exact per-dose match, not a bare timestamp compare ──────

def test_confirmed_dose_match_confirms():
    med = make_med(status="confirmed", last_taken=iso(local_dt(7, 32)),
                   confirmed_dose=iso(local_dt(8, 0)))
    assert already_confirmed(med, local_dt(8, 0)) is True


def test_different_occurrence_is_not_confirmed():
    # morning confirmed; the evening occurrence must not inherit it
    med = make_med(status="confirmed", last_taken=iso(local_dt(7, 32)),
                   confirmed_dose=iso(local_dt(8, 0)))
    assert already_confirmed(med, local_dt(20, 0)) is False


def test_stale_confirm_does_not_cover_next_day():
    med = make_med(status="confirmed", last_taken=iso(local_dt(8, 14)),
                   confirmed_dose=iso(local_dt(8, 0)))
    assert already_confirmed(med, local_dt(8, 0) + timedelta(days=1)) is False


def test_legacy_state_falls_back_to_last_taken_rule():
    med = make_med(status="confirmed", last_taken=iso(local_dt(8, 14)),
                   include_confirmed_dose=False)
    assert already_confirmed(med, local_dt(8, 0)) is True   # old behavior kept
    assert already_confirmed(med, local_dt(20, 0)) is False


# ── Bug #1 regression: early confirm suppresses the whole escalation ──────────

def test_fire_check_miss_all_skip_after_early_confirm():
    """
    Digest at 07:30, user replies 'all taken' at 07:32, dose at 08:00.
    The 08:00 fire, 08:30 check, and 09:30 miss must all skip.
    """
    med = make_med(frequency="twice_daily", times=["08:00", "20:00"],
                   status="confirmed", last_taken=iso(local_dt(7, 32)),
                   confirmed_dose=iso(local_dt(8, 0)))
    state = make_state(meds=[med])

    for mode, now in (("fire", local_dt(8, 0)),
                      ("check", local_dt(8, 30)),
                      ("miss", local_dt(9, 30))):
        dose_time = most_recent_dose_time(["08:00", "20:00"], 0, TZ, now)
        assert dose_time == local_dt(8, 0)
        assert already_confirmed(med, dose_time), f"{mode} must see the dose as confirmed"
        assert skip_if_needed(state, med, dose_time, mode) is True, f"{mode} must skip"


def test_handle_fire_after_early_confirm_sends_nothing_and_writes_nothing():
    med = make_med(frequency="twice_daily", times=["08:00", "20:00"],
                   status="confirmed", last_taken=iso(local_dt(7, 32)),
                   confirmed_dose=iso(local_dt(8, 0)))
    state = make_state(meds=[med])
    sent, saved = [], []
    with patch("dispatch.load_state", return_value=state), \
         patch("dispatch.save_state", side_effect=lambda s, dr: saved.append(s)), \
         patch("dispatch.send_message", side_effect=lambda t, dr: sent.append(t)), \
         patch("dispatch.datetime", fake_now(local_dt(8, 0))):
        handle_fire("med-001", 0, dry_run=True)
    assert sent == [] and saved == []


# ── Bug #4 regression: same-day second dose stays loggable ─────────────────────

def run_confirm(state, clock):
    saved = []
    with patch("reply.load_state", return_value=state), \
         patch("reply.save_state", side_effect=lambda s, dr: saved.append(s)), \
         patch("reply.datetime", fake_now(clock)):
        handle_confirm("med-001", None, dry_run=False)
    return saved


def test_evening_confirm_after_morning_confirm_succeeds():
    """
    Machine asleep at 20:00 — fire never ran, status still 'confirmed' from the
    morning dose. The 20:30 confirm must log the evening dose, not refuse.
    """
    med = make_med(frequency="twice_daily", times=["08:00", "20:00"],
                   status="confirmed", last_taken=iso(local_dt(8, 14)),
                   confirmed_dose=iso(local_dt(8, 0)))
    saved = run_confirm(make_state(meds=[med]), local_dt(20, 30))
    assert saved, "evening confirm must not be refused"
    st = saved[-1]["meds"][0]["state"]
    assert st["confirmed_dose"] == iso(local_dt(20, 0))
    assert st["next_due"] == iso(local_dt(8, 0) + timedelta(days=1))
    assert st["history"][-1]["event"] == "taken"


def test_double_confirm_of_same_dose_is_noop():
    med = make_med(frequency="once_daily", times=["08:00"],
                   status="confirmed", last_taken=iso(local_dt(8, 14)),
                   confirmed_dose=iso(local_dt(8, 0)))
    saved = run_confirm(make_state(meds=[med]), local_dt(8, 14))
    assert saved == []   # same occurrence already credited — no write


def test_early_confirm_anchors_next_due_past_covered_dose():
    med = make_med(frequency="twice_daily", times=["08:00", "20:00"])
    saved = run_confirm(make_state(meds=[med]), local_dt(7, 32))
    st = saved[-1]["meds"][0]["state"]
    assert st["confirmed_dose"] == iso(local_dt(8, 0))
    assert st["next_due"] == iso(local_dt(20, 0))   # not the 08:00 just covered
