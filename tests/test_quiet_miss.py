"""
Quiet-hours tests for handle_miss and the setup-tasks dose-in-quiet warning.

Regression: handle_miss used to ignore quiet hours entirely — a 'You missed
your dose' message could land at 3am. Now the miss is always logged (state,
history, next_due stay correct) but the outbound message is suppressed inside
the quiet window.
"""
from unittest.mock import patch

from dispatch import handle_miss
from tests.conftest import make_state, make_med, local_dt, fake_now, load_setup_tasks

setup_tasks = load_setup_tasks()


def run_miss(state, clock):
    sent, saved = [], []
    with patch("dispatch.load_state", return_value=state), \
         patch("dispatch.save_state", side_effect=lambda s, dr: saved.append(s)), \
         patch("dispatch.send_message", side_effect=lambda t, dr: sent.append(t)), \
         patch("dispatch.datetime", fake_now(clock)):
        handle_miss("med-001", 0, dry_run=False)
    return sent, saved


# ── handle_miss message suppression ────────────────────────────────────────────

def test_miss_during_quiet_hours_logs_without_message():
    med = make_med(med_id="med-001", status="reminded")   # unconfirmed
    state = make_state(meds=[med])
    sent, saved = run_miss(state, local_dt(23, 30))       # inside 22:00–07:00
    assert sent == [], "no outbound message during quiet hours"
    st = saved[-1]["meds"][0]["state"]
    assert st["status"] == "missed"
    assert st["missed_count"] == 1
    assert st["history"][-1]["event"] == "missed"


def test_miss_outside_quiet_hours_sends_message():
    med = make_med(med_id="med-001", status="reminded")
    state = make_state(meds=[med])
    sent, saved = run_miss(state, local_dt(12, 0))        # noon, dose was 08:00
    assert len(sent) == 1 and "Missed" in sent[0]
    assert saved[-1]["meds"][0]["state"]["status"] == "missed"


# ── setup-tasks: warn when a dose time sits inside quiet hours ────────────────

def test_in_quiet_window_matches():
    q = {"start": "22:00", "end": "07:00"}
    assert setup_tasks.in_quiet_window("23:00", q) is True
    assert setup_tasks.in_quiet_window("03:00", q) is True
    assert setup_tasks.in_quiet_window("22:00", q) is True    # start is quiet
    assert setup_tasks.in_quiet_window("07:00", q) is False   # end is not
    assert setup_tasks.in_quiet_window("12:00", q) is False


def test_in_quiet_window_same_day_window():
    q = {"start": "13:00", "end": "15:00"}
    assert setup_tasks.in_quiet_window("14:00", q) is True
    assert setup_tasks.in_quiet_window("12:59", q) is False


def test_build_tasks_warns_for_dose_inside_quiet_hours(capsys):
    med = make_med(med_id="med-001", times=["23:30"])
    setup_tasks.build_tasks(make_state(meds=[med]))
    out = capsys.readouterr().out
    assert "inside quiet hours" in out


def test_build_tasks_no_warning_for_normal_dose(capsys):
    med = make_med(med_id="med-001", times=["08:00"])
    setup_tasks.build_tasks(make_state(meds=[med]))
    assert "quiet hours" not in capsys.readouterr().out
