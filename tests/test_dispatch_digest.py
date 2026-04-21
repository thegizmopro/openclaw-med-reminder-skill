"""
Cases 18-20: handle_digest() — daily summary and deferred reset.
"""
from unittest.mock import patch, call
from datetime import datetime
from zoneinfo import ZoneInfo

from dispatch import handle_digest
from tests.conftest import make_state, make_med, iso, local_dt, TZ


def run_digest(state):
    """Run handle_digest with mocked I/O. Returns (sent_messages, saved_states)."""
    sent    = []
    saved   = []
    with patch("dispatch.load_state", return_value=state), \
         patch("dispatch.save_state",   side_effect=lambda s, dr: saved.append(s)), \
         patch("dispatch.send_message", side_effect=lambda t, dr: sent.append(t)):
        handle_digest(dry_run=False)
    return sent, saved


# ── Case 18: digest includes all non-paused meds ─────────────────────────────

def test_case_18_digest_includes_active_meds():
    met = make_med(med_id="med-001", name="Metformin",
                   frequency="once_daily", times=["08:00"])
    lis = make_med(med_id="med-002", name="Lisinopril",
                   frequency="once_daily", times=["21:00"])
    state = make_state(meds=[met, lis])

    sent, _ = run_digest(state)

    assert len(sent) == 1, "Digest should send exactly one message"
    msg = sent[0]
    assert "Metformin" in msg
    assert "Lisinopril" in msg


# ── Case 19: paused med excluded from digest ──────────────────────────────────

def test_case_19_paused_med_excluded_from_digest():
    active = make_med(med_id="med-001", name="Metformin",
                      frequency="once_daily", times=["08:00"], paused=False)
    paused = make_med(med_id="med-002", name="Lisinopril",
                      frequency="once_daily", times=["21:00"], paused=True)
    state = make_state(meds=[active, paused])

    sent, _ = run_digest(state)

    assert len(sent) == 1
    msg = sent[0]
    assert "Metformin"  in msg
    assert "Lisinopril" not in msg


# ── Case 20: deferred meds reset to pending when digest fires ─────────────────

def test_case_20_deferred_reset_to_pending_on_digest():
    deferred = make_med(med_id="med-001", name="Metformin",
                        frequency="once_daily", times=["08:00"],
                        status="deferred")
    state = make_state(meds=[deferred])

    _, saved = run_digest(state)

    assert saved, "State should have been saved after resetting deferred med"
    final_state = saved[-1]
    assert final_state["meds"][0]["state"]["status"] == "pending", \
        "Deferred med should be reset to pending after digest"


# ── Extra: global pause skips digest entirely ─────────────────────────────────

def test_global_pause_skips_digest():
    met   = make_med(med_id="med-001", name="Metformin",
                     frequency="once_daily", times=["08:00"])
    state = make_state(paused=True, meds=[met])

    sent, saved = run_digest(state)

    assert len(sent) == 0,  "No message should be sent when globally paused"
    assert len(saved) == 0, "State should not be written when globally paused"


# ── Extra: empty meds list → no message, no save ─────────────────────────────

def test_empty_meds_no_message():
    state = make_state(meds=[])
    sent, saved = run_digest(state)
    assert len(sent) == 0


# ── Extra: as_needed meds appear in digest ────────────────────────────────────

def test_as_needed_med_shown_in_digest():
    med   = make_med(med_id="med-001", name="Ibuprofen", frequency="as_needed")
    state = make_state(meds=[med])
    sent, _ = run_digest(state)
    assert "Ibuprofen" in sent[0]
    assert "as needed" in sent[0]
