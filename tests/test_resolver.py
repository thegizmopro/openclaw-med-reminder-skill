"""
Cases 1-10: escalation resolver tiers.

resolve(state, med, now) is defined here as a thin wrapper over dispatch
primitives — it mirrors the logic described in the design doc:
  QUIET_PAUSED | QUIET_HOURS | PENDING | ON_TIME | GRACE | LATE | MISSED
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dispatch import already_confirmed, in_quiet_hours, get_tz
from tests.conftest import make_state, make_med, local_dt, iso, TZ


# ── Local resolver ─────────────────────────────────────────────────────────────

def resolve(state, med, now):
    g = state["global"]
    tz = get_tz(state)

    if g.get("paused") or med.get("paused"):
        return "QUIET_PAUSED"
    if in_quiet_hours(now, g["quiet_hours"], tz):
        return "QUIET_HOURS"
    if med["state"]["next_due"] is None:
        return "PENDING"

    dose_time = datetime.fromisoformat(med["state"]["next_due"])
    if already_confirmed(med, dose_time):
        return "ON_TIME"

    delta = (now - dose_time).total_seconds() / 60
    if delta < 0:
        return "ON_TIME"

    late = med["escalation"]["late_threshold_minutes"]
    missed = med["escalation"]["missed_threshold_minutes"]

    if delta < late:
        return "GRACE"
    if delta < missed:
        return "LATE"
    return "MISSED"


# ── Helpers ────────────────────────────────────────────────────────────────────

def med_with_next_due(delta_minutes, late=30, missed=90):
    """Med whose next_due is `delta_minutes` in the past (positive = overdue)."""
    now = local_dt(12, 0)  # noon, well outside quiet hours
    next_due = now - timedelta(minutes=delta_minutes)
    return (
        make_state(),
        make_med(next_due=iso(next_due), late_threshold=late, missed_threshold=missed),
        now,
    )


# ── Cases 1-2: pause ───────────────────────────────────────────────────────────

def test_case_01_global_pause():
    state = make_state(paused=True)
    med   = make_med(next_due=iso(local_dt(11, 0)))
    now   = local_dt(12, 0)
    assert resolve(state, med, now) == "QUIET_PAUSED"


def test_case_02_med_pause():
    state = make_state()
    med   = make_med(paused=True, next_due=iso(local_dt(11, 0)))
    now   = local_dt(12, 0)
    assert resolve(state, med, now) == "QUIET_PAUSED"


# ── Cases 3-5: quiet hours (22:00–07:00, midnight-spanning) ───────────────────

def test_case_03_inside_quiet_hours():
    state = make_state()
    med   = make_med(next_due=iso(local_dt(22, 30)))
    now   = local_dt(23, 0)  # 11 pm — inside quiet window
    assert resolve(state, med, now) == "QUIET_HOURS"


def test_case_04_exact_quiet_start_is_quiet():
    """22:00 exactly is the start of the quiet window — should be quiet."""
    state = make_state()
    med   = make_med(next_due=iso(local_dt(21, 0)))
    now   = local_dt(22, 0)  # exact start boundary
    assert resolve(state, med, now) == "QUIET_HOURS"


def test_case_05_exact_quiet_end_is_not_quiet():
    """07:00 exactly is the end of the quiet window — should NOT be quiet."""
    state = make_state()
    med   = make_med(next_due=iso(local_dt(6, 0)))
    now   = local_dt(7, 0)   # exact end boundary
    assert resolve(state, med, now) != "QUIET_HOURS"


# ── Case 6: next_due is null ───────────────────────────────────────────────────

def test_case_06_next_due_null_is_pending():
    state = make_state()
    med   = make_med(next_due=None)  # never initialized
    now   = local_dt(12, 0)
    assert resolve(state, med, now) == "PENDING"


# ── Case 7: not yet due ────────────────────────────────────────────────────────

def test_case_07_not_yet_due_is_on_time():
    state, med, now = med_with_next_due(delta_minutes=-30)  # due in 30 min
    assert resolve(state, med, now) == "ON_TIME"


# ── Case 8: within grace window ───────────────────────────────────────────────

def test_case_08_within_grace_is_grace():
    """15 min overdue with 30 min late threshold → GRACE (no message)."""
    state, med, now = med_with_next_due(delta_minutes=15, late=30, missed=90)
    assert resolve(state, med, now) == "GRACE"


def test_case_08b_exact_zero_delta_is_grace():
    """Exactly on time (delta=0) → GRACE, not LATE."""
    state, med, now = med_with_next_due(delta_minutes=0, late=30, missed=90)
    assert resolve(state, med, now) == "GRACE"


# ── Case 9: late window ────────────────────────────────────────────────────────

def test_case_09_late_window_sends_late():
    """45 min overdue, late=30, missed=90 → LATE."""
    state, med, now = med_with_next_due(delta_minutes=45, late=30, missed=90)
    assert resolve(state, med, now) == "LATE"


def test_case_09b_exact_late_threshold_boundary():
    """Exactly at late threshold → LATE, not GRACE."""
    state, med, now = med_with_next_due(delta_minutes=30, late=30, missed=90)
    assert resolve(state, med, now) == "LATE"


# ── Case 10: missed ────────────────────────────────────────────────────────────

def test_case_10_past_missed_threshold_is_missed():
    """120 min overdue, missed=90 → MISSED."""
    state, med, now = med_with_next_due(delta_minutes=120, late=30, missed=90)
    assert resolve(state, med, now) == "MISSED"


def test_case_10b_exact_missed_threshold_boundary():
    """Exactly at missed threshold → MISSED."""
    state, med, now = med_with_next_due(delta_minutes=90, late=30, missed=90)
    assert resolve(state, med, now) == "MISSED"
