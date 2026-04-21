"""
Reply handler tests — dispatch.py confirm and defer subcommands.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from dispatch import handle_confirm, handle_confirm_all, handle_defer
from tests.conftest import make_state, make_med, iso, local_dt, TZ


def run_confirm(state, med_id, dose_taken=None):
    saved = []
    with patch("dispatch.load_state", return_value=state), \
         patch("dispatch.save_state", side_effect=lambda s, dr: saved.append(s)):
        handle_confirm(med_id, dose_taken, dry_run=False)
    return saved


def run_confirm_all(state, dose_taken=None):
    saved = []
    with patch("dispatch.load_state", return_value=state), \
         patch("dispatch.save_state", side_effect=lambda s, dr: saved.append(s)):
        handle_confirm_all(dose_taken, dry_run=False)
    return saved


def run_defer(state, med_id):
    saved = []
    with patch("dispatch.load_state", return_value=state), \
         patch("dispatch.save_state", side_effect=lambda s, dr: saved.append(s)):
        handle_defer(med_id, dry_run=False)
    return saved


# ── confirm: status and last_taken ────────────────────────────────────────────

def test_confirm_sets_status_confirmed():
    med   = make_med(med_id="med-001", status="reminded")
    state = make_state(meds=[med])
    saved = run_confirm(state, "med-001")
    assert saved[-1]["meds"][0]["state"]["status"] == "confirmed"


def test_confirm_sets_last_taken_to_now():
    med   = make_med(med_id="med-001", status="reminded")
    state = make_state(meds=[med])
    before = datetime.now(tz=TZ)
    saved  = run_confirm(state, "med-001")
    after  = datetime.now(tz=TZ)
    last_taken = datetime.fromisoformat(saved[-1]["meds"][0]["state"]["last_taken"])
    assert before <= last_taken <= after


# ── confirm: next_due is advanced ─────────────────────────────────────────────

def test_confirm_advances_next_due_once_daily():
    med   = make_med(med_id="med-001", frequency="once_daily", times=["08:00"],
                     next_due=iso(local_dt(8, 0)))
    state = make_state(meds=[med])
    saved = run_confirm(state, "med-001")
    nd    = saved[-1]["meds"][0]["state"]["next_due"]
    assert nd is not None
    nd_dt = datetime.fromisoformat(nd)
    assert nd_dt > datetime.now(tz=TZ), "next_due must be in the future"


def test_confirm_sets_next_due_none_for_as_needed():
    med   = make_med(med_id="med-001", frequency="as_needed", status="pending")
    state = make_state(meds=[med])
    saved = run_confirm(state, "med-001")
    assert saved[-1]["meds"][0]["state"]["next_due"] is None


# ── confirm: history entry ─────────────────────────────────────────────────────

def test_confirm_appends_taken_history():
    med   = make_med(med_id="med-001")
    state = make_state(meds=[med])
    saved = run_confirm(state, "med-001")
    hist  = saved[-1]["meds"][0]["state"]["history"]
    assert len(hist) == 1
    assert hist[-1]["event"] == "taken"


def test_confirm_stores_dose_taken_when_provided():
    med   = make_med(med_id="med-001", dose="500", unit="mg")
    state = make_state(meds=[med])
    saved = run_confirm(state, "med-001", dose_taken="250mg")
    assert saved[-1]["meds"][0]["state"]["history"][-1]["dose_taken"] == "250mg"


def test_confirm_dose_taken_none_when_not_provided():
    med   = make_med(med_id="med-001")
    state = make_state(meds=[med])
    saved = run_confirm(state, "med-001")
    assert saved[-1]["meds"][0]["state"]["history"][-1]["dose_taken"] is None


# ── confirm: paused med is skipped ────────────────────────────────────────────

def test_confirm_skips_paused_med():
    med   = make_med(med_id="med-001", paused=True)
    state = make_state(meds=[med])
    saved = run_confirm(state, "med-001")
    assert len(saved) == 0, "No state write should occur for a paused med"


# ── confirm --all ─────────────────────────────────────────────────────────────

def test_confirm_all_marks_all_pending():
    m1 = make_med(med_id="med-001", name="Metformin",  status="reminded")
    m2 = make_med(med_id="med-002", name="Lisinopril", status="late")
    state = make_state(meds=[m1, m2])
    saved = run_confirm_all(state)
    result = saved[-1]["meds"]
    assert result[0]["state"]["status"] == "confirmed"
    assert result[1]["state"]["status"] == "confirmed"


def test_confirm_all_skips_paused():
    active = make_med(med_id="med-001", name="Metformin",  paused=False, status="reminded")
    paused = make_med(med_id="med-002", name="Lisinopril", paused=True,  status="reminded")
    state  = make_state(meds=[active, paused])
    saved  = run_confirm_all(state)
    result = saved[-1]["meds"]
    assert result[0]["state"]["status"] == "confirmed"
    assert result[1]["state"]["status"] == "reminded"  # unchanged


def test_confirm_all_skips_already_confirmed():
    m1 = make_med(med_id="med-001", name="Metformin",  status="confirmed")
    m2 = make_med(med_id="med-002", name="Lisinopril", status="late")
    state = make_state(meds=[m1, m2])
    saved = run_confirm_all(state)
    result = saved[-1]["meds"]
    assert result[0]["state"]["status"] == "confirmed"  # was already confirmed, unchanged
    assert result[1]["state"]["status"] == "confirmed"  # newly confirmed


def test_confirm_all_no_save_when_nothing_to_confirm():
    m1 = make_med(med_id="med-001", status="confirmed")
    state = make_state(meds=[m1])
    saved = run_confirm_all(state)
    assert len(saved) == 0


# ── defer ─────────────────────────────────────────────────────────────────────

def test_defer_sets_status_deferred():
    med   = make_med(med_id="med-001", status="reminded")
    state = make_state(meds=[med])
    saved = run_defer(state, "med-001")
    assert saved[-1]["meds"][0]["state"]["status"] == "deferred"


def test_defer_appends_deferred_history():
    med   = make_med(med_id="med-001")
    state = make_state(meds=[med])
    saved = run_defer(state, "med-001")
    hist  = saved[-1]["meds"][0]["state"]["history"]
    assert hist[-1]["event"] == "deferred"


def test_defer_paused_med_does_not_write():
    med   = make_med(med_id="med-001", paused=True)
    state = make_state(meds=[med])
    saved = run_defer(state, "med-001")
    assert len(saved) == 0


# ── dry-run: no save called ───────────────────────────────────────────────────

def test_confirm_dry_run_does_not_call_save():
    med   = make_med(med_id="med-001")
    state = make_state(meds=[med])
    saved = []
    with patch("dispatch.load_state", return_value=state), \
         patch("dispatch.save_state", side_effect=lambda s, dr: saved.append(s) if not dr else None):
        handle_confirm("med-001", None, dry_run=True)
    assert len(saved) == 0
